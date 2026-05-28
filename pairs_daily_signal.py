"""Daily pairs-trade opportunity alert -> Slack (internal workspace only).

Monitors a curated candidate set of same-sector pairs (the correlation-passers
from the EPAT pairs screen). Each run, on a trailing 2y window it:
  - estimates hedge ratio beta (OLS of logA on logB),
  - re-validates cointegration (Engle-Granger p<0.05 + ADF on residual p<0.05),
  - confirms a tradable OU half-life (5..30 trading days),
  - computes the CURRENT rolling-60d z-score of the spread.

An OPPORTUNITY fires when a still-cointegrated pair's |z| is in the entry zone
(>=2.0) but not past the stop (<3.5). Only firing pairs are posted to Slack,
each with entry / target / stop-loss / time-stop and beta-neutral lot ratios.
If nothing fires, the script stays silent (no Slack spam).

Compliance: posts to the internal Slack workspace ONLY. Do NOT route pair
trade calls to Telegram / public channels (SEBI RA/RIA constraint).

Env: FYERS_CLIENT_ID, SLACK_BOT_TOKEN, SLACK_CHANNEL  (SLACK_WEBHOOK_URL optional)
     (Fyers token produced by auto_login.py earlier in the workflow.)
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller, coint
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
CID = os.getenv("FYERS_CLIENT_ID")

# --- candidate pairs to monitor (sector, A, B). Re-validated each run. ---
CANDIDATE_PAIRS = [
    ("PSU Banks", "BANKBARODA", "CANBK"),
    ("Metals", "TATASTEEL", "JINDALSTEL"),
    ("Cement", "AMBUJACEM", "ACC"),
    ("IT", "TCS", "INFY"),
    ("PSU Banks", "BANKBARODA", "PNB"),
    ("Oil & Gas", "IOC", "BPCL"),
    ("Metals", "TATASTEEL", "SAIL"),
    ("Metals", "NMDC", "SAIL"),
    ("Metals", "TATASTEEL", "NATIONALUM"),
    ("Metals", "SAIL", "NATIONALUM"),
    ("NBFC", "BAJFINANCE", "BAJAJFINSV"),
    ("Oil & Gas", "BPCL", "HPCL"),
    ("Metals", "TATASTEEL", "JSWSTEEL"),
    ("PSU Banks", "SBIN", "BANKBARODA"),
    ("Oil & Gas", "IOC", "HPCL"),
    ("PSU Banks", "SBIN", "CANBK"),
    ("PSU Banks", "UNIONBANK", "BANKINDIA"),
    ("PSU Banks", "PNB", "CANBK"),
    ("PSU Banks", "CANBK", "UNIONBANK"),
]

# display label -> actual Fyers/NSE ticker (renames + demerger)
TICKER_MAP = {"HPCL": "HINDPETRO", "MAXFIN": "MFSL", "TATAMOTORS": "TMPV"}

# Thresholds (mirror the EPAT screen)
COINT_P_MAX = 0.05
ADF_P_MAX = 0.05
HL_MIN, HL_MAX = 5, 30
CV_MAX = 0.20          # used only as a risk FLAG, not a hard filter
ROLL_BETA_WIN = 60
Z_WIN = 60
Z_ENTRY, Z_EXIT, Z_STOP = 2.0, 0.5, 3.5
INSAMPLE_DAYS = 365 * 2   # trailing 2y for beta/cointegration


def fy_symbol(stock: str) -> str:
    return f"NSE:{TICKER_MAP.get(stock, stock)}-EQ"


def fetch_daily(fy, stock, years=2.6):
    sym = fy_symbol(stock)
    end = dt.date.today()
    start = end - dt.timedelta(days=int(years * 365.25) + 5)
    frames, cur = [], start
    while cur < end:
        ce = min(cur + dt.timedelta(days=360), end)
        try:
            r = fy.history({"symbol": sym, "resolution": "D", "date_format": "1",
                            "range_from": cur.strftime("%Y-%m-%d"),
                            "range_to": ce.strftime("%Y-%m-%d"), "cont_flag": "1"})
        except Exception as e:
            print(f"  {stock}: history error {e}", file=sys.stderr)
            r = {}
        if r.get("s") == "ok" and r.get("candles"):
            frames.append(pd.DataFrame(r["candles"],
                          columns=["ts", "o", "h", "l", "close", "v"]))
        cur = ce + dt.timedelta(days=1)
        time.sleep(0.3)
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("ts").sort_values("ts")
    df["date"] = (pd.to_datetime(df["ts"], unit="s").dt.tz_localize("UTC")
                  .dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None))
    return df.set_index("date")["close"]


def ols_beta(logA, logB):
    r = sm.OLS(logA.values, sm.add_constant(logB.values)).fit()
    a, b = r.params[0], r.params[1]
    resid = pd.Series(logA.values - (a + b * logB.values), index=logA.index)
    return b, a, resid


def half_life(resid):
    s = resid.dropna(); ds = s.diff().dropna(); lag = s.shift().dropna()
    lag, ds = lag.align(ds, join="inner")
    res = sm.OLS(ds.values, sm.add_constant(lag.values)).fit()
    lam = res.params[1]
    return (-np.log(2) / lam) if lam < 0 else np.inf


def beta_cv(logA, logB, win=ROLL_BETA_WIN):
    a, b = logA.values, logB.values
    betas = [sm.OLS(a[i:i+win], sm.add_constant(b[i:i+win])).fit().params[1]
             for i in range(0, len(a) - win + 1)]
    betas = np.array(betas)
    if len(betas) == 0:
        return np.nan
    m, sd = betas.mean(), betas.std(ddof=1)
    return sd / abs(m) if m else np.inf


def get_lot_sizes(stocks):
    """Near-month F&O lot sizes from the Fyers public master."""
    try:
        r = requests.get("https://public.fyers.in/sym_details/NSE_FO.csv", timeout=60)
        rows = [ln.split(",") for ln in r.text.splitlines() if ln.strip()]
    except Exception as e:
        print(f"  lot-size fetch failed: {e}", file=sys.stderr)
        return {}
    import re
    lots = {}
    for s in stocks:
        und = TICKER_MAP.get(s, s)
        best = None
        for p in rows:
            if len(p) > 13 and p[13] == und and p[9].endswith("FUT"):
                m = re.search(r"(\d{1,2} [A-Za-z]{3} \d{2}) FUT", p[1] or "")
                if not m:
                    continue
                exp = pd.to_datetime(m.group(1), format="%d %b %y", errors="coerce")
                lot = pd.to_numeric(p[3], errors="coerce")
                if pd.notna(exp) and pd.notna(lot):
                    if best is None or exp < best[0]:
                        best = (exp, int(lot))
        if best:
            lots[s] = best[1]
    return lots


def hedge_lot_ratio(beta, lotA, lotB, priceA, priceB):
    """Approximate beta-neutral whole-lot ratio nA:nB.

    For a log-price spread (logA - beta*logB), the value-neutral hedge holds
    value_B = beta * value_A, i.e. (nB*lotB*priceB) = beta*(nA*lotA*priceA).
    Search small whole-lot combos to best match that value ratio."""
    from math import gcd
    valA = lotA * priceA
    valB = lotB * priceB
    best = None
    for nB in range(1, 13):
        for nA in range(1, 13):
            value_ratio = (nB * valB) / (nA * valA)  # want ~= beta
            err = abs(value_ratio - beta)
            if best is None or err < best[0]:
                best = (err, nA, nB)
    _, nA, nB = best
    g = gcd(nA, nB)
    return nA // g, nB // g


def analyse_pair(sector, A, B, px):
    if A not in px.columns or B not in px.columns:
        return None
    a = px[A].dropna(); b = px[B].dropna()
    j = a.index.intersection(b.index)
    if len(j) < 300:
        return None
    a, b = a.loc[j], b.loc[j]
    # trailing 2y window for beta / cointegration
    cutoff = a.index.max() - pd.Timedelta(days=INSAMPLE_DAYS)
    ins = a.index >= cutoff
    la_full, lb_full = np.log(a), np.log(b)
    la, lb = la_full[ins], lb_full[ins]
    if len(la) < 250:
        return None
    try:
        _, coint_p, _ = coint(la.values, lb.values)
    except Exception:
        return None
    beta, alpha, resid = ols_beta(la, lb)
    try:
        adf_p = float(adfuller(resid.dropna().values, autolag="AIC")[1])
    except Exception:
        adf_p = np.nan
    hl = half_life(resid)
    cv = beta_cv(la, lb)

    # current z on FULL series spread, rolling 60d
    spread = la_full - (alpha + beta * lb_full)
    mu = spread.rolling(Z_WIN).mean()
    sd = spread.rolling(Z_WIN).std()
    z = (spread - mu) / sd
    z_now = float(z.iloc[-1])
    mu_now = float(mu.iloc[-1]); sd_now = float(sd.iloc[-1])
    spread_now = float(spread.iloc[-1])

    cointegrated = (coint_p < COINT_P_MAX) and (np.isfinite(adf_p) and adf_p < ADF_P_MAX)
    hl_ok = np.isfinite(hl) and HL_MIN <= hl <= HL_MAX
    in_entry = abs(z_now) >= Z_ENTRY and abs(z_now) < Z_STOP

    return {
        "sector": sector, "A": A, "B": B, "beta": beta, "alpha": alpha,
        "coint_p": coint_p, "adf_p": adf_p, "half_life": hl, "beta_cv": cv,
        "z_now": z_now, "mu": mu_now, "sd": sd_now, "spread_now": spread_now,
        "priceA": float(a.iloc[-1]), "priceB": float(b.iloc[-1]),
        "cointegrated": cointegrated, "hl_ok": hl_ok, "in_entry": in_entry,
        "last_date": a.index.max().date().isoformat(),
    }


def build_signal(r, lots):
    """Return entry/target/SL detail dict for a firing opportunity."""
    z = r["z_now"]; beta = r["beta"]; sd = r["sd"]
    gross = 1.0 + abs(beta)
    # direction: fade the deviation
    short_spread = z > 0  # spread rich -> short A / long B
    # spread levels at target (z=±0.5 toward mean) and stop (z=±3.5)
    sgn = 1 if z > 0 else -1
    spread_target = r["mu"] + sgn * Z_EXIT * sd
    spread_stop = r["mu"] + sgn * Z_STOP * sd
    # expected % move on notional (log-spread change / gross exposure)
    move_to_target = abs(r["spread_now"] - spread_target) / gross
    move_to_stop = abs(spread_stop - r["spread_now"]) / gross
    lotA = lots.get(r["A"]); lotB = lots.get(r["B"])
    ratio = (hedge_lot_ratio(beta, lotA, lotB, r["priceA"], r["priceB"])
             if (lotA and lotB) else None)
    return {
        "short_spread": short_spread,
        "spread_target": spread_target, "spread_stop": spread_stop,
        "exp_target_pct": move_to_target * 100, "exp_stop_pct": move_to_stop * 100,
        "lotA": lotA, "lotB": lotB, "ratio": ratio,
        "time_stop_days": int(round(2 * r["half_life"])) if np.isfinite(r["half_life"]) else None,
    }


def fmt_blocks(opps, asof):
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"⚠️ Pairs-Trade Opportunity — {asof}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "Mean-reversion entry signal · spread z-score ≥ 2 on a "
                 "cointegrated pair · _internal use only, not investment advice_"}]},
    ]
    for r, s in opps:
        side = "SHORT spread" if s["short_spread"] else "LONG spread"
        if s["short_spread"]:
            legA = f"SELL *{r['A']}* @ ~{r['priceA']:,.2f}"
            legB = f"BUY  *{r['B']}* @ ~{r['priceB']:,.2f}"
        else:
            legA = f"BUY  *{r['A']}* @ ~{r['priceA']:,.2f}"
            legB = f"SELL *{r['B']}* @ ~{r['priceB']:,.2f}"
        ratio_txt = ""
        if s["ratio"] and s["lotA"] and s["lotB"]:
            nA, nB = s["ratio"]
            valA = nA * s["lotA"] * r["priceA"]
            valB = nB * s["lotB"] * r["priceB"]
            ratio_txt = (f"\n   β-neutral lots (β≈{r['beta']:.2f}): "
                         f"*{nA}× {r['A']}* (₹{valA/1e5:,.1f}L)  :  "
                         f"*{nB}× {r['B']}* (₹{valB/1e5:,.1f}L)  "
                         f"[scale down proportionally for smaller size]")
        elif s["lotA"] and s["lotB"]:
            ratio_txt = f"\n   Lot sizes: {r['A']} {s['lotA']} · {r['B']} {s['lotB']}"
        flag = ""
        if not (np.isfinite(r["beta_cv"]) and r["beta_cv"] <= CV_MAX):
            flag = (f"\n   :warning: β unstable (CV {r['beta_cv']:.2f}) — "
                    f"consider rolling hedge / smaller size")
        ts = f"{s['time_stop_days']} trading days" if s["time_stop_days"] else "—"
        txt = (
            f"*{r['A']} / {r['B']}* ({r['sector']})\n"
            f"Signal: *{side}*  ·  z = {r['z_now']:+.2f}  "
            f"[β {r['beta']:.2f} · half-life {r['half_life']:.0f}d · "
            f"coint p {r['coint_p']:.3f}]\n"
            f"   {legA}\n   {legB}{ratio_txt}\n"
            f"*Targets:*\n"
            f"   • Exit (target): z → ±0.5  (≈ +{s['exp_target_pct']:.2f}% on spread)\n"
            f"   • Stop-loss:     z → ±3.5  (≈ −{s['exp_stop_pct']:.2f}% on spread)\n"
            f"   • Time stop:     {ts} (2× half-life){flag}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "Manage on z-score: exit at |z|≤0.5, hard stop at |z|≥3.5, or time-stop. "
                "Spreads on weakly-cointegrated pairs can keep widening."}]})
    return blocks


def post_slack(blocks):
    token = os.environ.get("SLACK_BOT_TOKEN"); ch = os.environ.get("SLACK_CHANNEL")
    if not (token and ch):
        print("No Slack creds — printing instead.", file=sys.stderr)
        return
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"channel": ch, "blocks": blocks,
                            "text": "Pairs-trade opportunity"}, timeout=30)
    j = r.json() if r.ok else {}
    if j.get("ok"):
        print(f"Posted {j.get('ts')} to Slack channel.")
    else:
        print(f"Slack post failed: {r.status_code} {r.text[:300]}", file=sys.stderr)


def main():
    fy = fyersModel.FyersModel(client_id=CID,
                               token=(HERE / "access_token.txt").read_text().strip(),
                               log_path="")
    stocks = sorted({s for _, a, b in CANDIDATE_PAIRS for s in (a, b)})
    print(f"Fetching {len(stocks)} stocks for {len(CANDIDATE_PAIRS)} candidate pairs...")
    series = {}
    for s in stocks:
        px = fetch_daily(fy, s)
        if px is not None and len(px) > 300:
            series[s] = px
        else:
            print(f"  {s}: insufficient data — skipped", file=sys.stderr)
    px = pd.DataFrame(series).sort_index()

    analysed = []
    for sector, A, B in CANDIDATE_PAIRS:
        r = analyse_pair(sector, A, B, px)
        if r:
            analysed.append(r)
            tag = ("OPP" if (r["cointegrated"] and r["hl_ok"] and r["in_entry"]) else "-")
            print(f"  {A}/{B}: z={r['z_now']:+.2f} coint_p={r['coint_p']:.3f} "
                  f"hl={r['half_life']:.1f} cointeg={r['cointegrated']} "
                  f"entry={r['in_entry']} [{tag}]")

    # Opportunities: still cointegrated + tradable half-life + live entry-zone z
    opps_rows = [r for r in analysed
                 if r["cointegrated"] and r["hl_ok"] and r["in_entry"]]
    # rank by how deep into the entry zone (|z|), strongest first
    opps_rows.sort(key=lambda r: abs(r["z_now"]), reverse=True)

    if not opps_rows:
        print("No opportunities today — staying silent (no Slack post).")
        return 0

    lots = get_lot_sizes([s for r in opps_rows for s in (r["A"], r["B"])])
    opps = [(r, build_signal(r, lots)) for r in opps_rows]
    asof = analysed[0]["last_date"] if analysed else dt.date.today().isoformat()
    blocks = fmt_blocks(opps, asof)
    post_slack(blocks)
    print(f"Posted {len(opps)} opportunity(ies).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
