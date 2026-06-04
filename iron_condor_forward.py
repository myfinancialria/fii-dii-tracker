"""Daily intraday Iron Condor forward test on Nifty + Sensex.

STRATEGY (per user spec, identical for both indices):
  - At 10:00 IST:
      SELL 5-strikes-OTM CE  (e.g. ATM + 5×strike_interval)
      SELL 5-strikes-OTM PE  (e.g. ATM − 5×strike_interval)
      BUY  9-strikes-OTM CE  (hedge,  ATM + 9×strike_interval)
      BUY  9-strikes-OTM PE  (hedge,  ATM − 9×strike_interval)
  - Each sell leg has a 35% premium stop-loss (premium ≥ 1.35 × entry).
  - If a SL-stopped sell leg's premium returns to the original entry, RE-ENTER
    (sell again at the original entry premium). Multiple SL/re-entry cycles
    per leg per day are allowed.
  - SQUARE OFF all open legs at the 15:00 5-min candle close.
  - Trades the NEAREST WEEKLY EXPIRY each day (Tue for Nifty / Thu for Sensex).

EXECUTION MODEL:
  - This script runs ONCE post-close (15:15 IST cron). It fetches the day's
    5-min intraday history for the underlying and for each of the 4 option
    legs and REPLAYS the day deterministically (entry@10:00 candle open,
    SL/re-entry checks on every 5-min H/L from 10:05 onward, exit@15:00 close).
  - This uses REAL traded option premiums, because contracts that haven't
    yet expired ARE retrievable from Fyers.

CHARGES (Indian intraday options, applied per leg event):
  - Brokerage: ₹20 / order
  - STT: 0.0625% on sell premium turnover (sell-side only)
  - Exchange txn: 0.053% NSE / 0.0325% BSE on total premium turnover
  - SEBI: 0.0001% on total turnover
  - Stamp duty: 0.003% on buy-side turnover
  - GST: 18% on (brokerage + exchange txn + SEBI)

PERSISTENCE (committed to repo each run, like the pairs ledger):
  - iron_condor_daily.csv    — one row per day per index with full breakdown
  - iron_condor_trades.csv   — one row per event (entry / SL / re-entry / exit)

OUTPUT:
  - Slack post to the internal channel with the day's setup and result.
  - Silent on holidays / no-market days.

COMPLIANCE: Slack-internal only. Do NOT route to Telegram/public channels
(SEBI RA/RIA regulation — user holds NISM but not SEBI RA/RIA registration).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
CID = os.getenv("FYERS_CLIENT_ID")

# --------------------------- config ---------------------------
INDICES: dict[str, dict] = {
    "NIFTY": {
        "underlying": "NSE:NIFTY50-INDEX",
        "prefix":     "NSE:NIFTY",
        "strike_int": 50,
        "lot":        75,
        "exchange":   "NSE",
    },
    "SENSEX": {
        "underlying": "BSE:SENSEX-INDEX",
        "prefix":     "BSE:SENSEX",
        "strike_int": 100,
        "lot":        20,
        "exchange":   "BSE",
    },
}

OTM_SELL  = 5             # 5 strikes OTM for short legs
OTM_HEDGE = 9             # 9 strikes OTM for long hedges
SL_PCT    = 0.35          # 35% premium loss on sell leg => SL
ENTRY_T   = "10:00"
EXIT_T    = "15:00"
LOTS      = int(os.environ.get("IC_LOTS", "1"))   # start with 1 lot for paper test
CAPITAL_PER_INDEX = 10_00_000   # ₹10L per index (for utilisation %)

# charges
BROKERAGE_PER_ORDER = 20
STT_SELL_RATE = 0.000625        # 0.0625% on sell-side option premium
TXN_RATE = {"NSE": 0.00053, "BSE": 0.000325}
SEBI_RATE = 0.000001            # 0.0001%
STAMP_BUY_RATE = 0.00003        # 0.003% on buy-side
GST_RATE = 0.18

DAILY_CSV  = HERE / "iron_condor_daily.csv"
TRADES_CSV = HERE / "iron_condor_trades.csv"


# --------------------------- fyers helpers ---------------------------
def fy_client() -> fyersModel.FyersModel:
    tok = (HERE / "access_token.txt").read_text().strip()
    return fyersModel.FyersModel(client_id=CID, token=tok, log_path="")


def fetch_5min(fy: fyersModel.FyersModel, symbol: str,
               day: dt.date) -> pd.DataFrame | None:
    try:
        r = fy.history({
            "symbol": symbol, "resolution": "5", "date_format": "1",
            "range_from": day.strftime("%Y-%m-%d"),
            "range_to":   day.strftime("%Y-%m-%d"),
            "cont_flag":  "1",
        })
    except Exception as e:
        print(f"  history error {symbol}: {e}", file=sys.stderr)
        return None
    if r.get("s") != "ok" or not r.get("candles"):
        return None
    df = pd.DataFrame(r["candles"], columns=["ts","o","h","l","c","v"])
    df["dt"] = (pd.to_datetime(df["ts"], unit="s").dt.tz_localize("UTC")
                .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    df["t"] = df["dt"].dt.strftime("%H:%M")
    df = df[df["dt"].dt.date == day].reset_index(drop=True)
    return df if len(df) else None


def get_nearest_expiry(fy: fyersModel.FyersModel, idx: str) -> str | None:
    cfg = INDICES[idx]
    try:
        ch = fy.optionchain(data={"symbol": cfg["underlying"],
                                  "strikecount": 2, "timestamp": ""})
    except Exception as e:
        print(f"  optionchain {idx} failed: {e}", file=sys.stderr)
        return None
    if ch.get("code") != 200:
        return None
    eds = ch["data"].get("expiryData", [])
    return eds[0]["date"] if eds else None  # 'DD-MM-YYYY'


def build_symbols(prefix: str, expiry_ddmmyyyy: str,
                  atm: int, strike_int: int) -> dict[str, str]:
    """Format: <PREFIX><YY><M><DD><STRIKE><CE|PE>   e.g. NSE:NIFTY2660923300CE
    Month is a single digit 1-9 (no leading zero); day is 2 digits."""
    d, m, y = expiry_ddmmyyyy.split("-")
    yy = y[-2:]
    mm = str(int(m))
    dd = d.zfill(2)
    sfx = f"{yy}{mm}{dd}"
    return {
        "sell_ce": f"{prefix}{sfx}{atm + OTM_SELL * strike_int}CE",
        "sell_pe": f"{prefix}{sfx}{atm - OTM_SELL * strike_int}PE",
        "buy_ce":  f"{prefix}{sfx}{atm + OTM_HEDGE * strike_int}CE",
        "buy_pe":  f"{prefix}{sfx}{atm - OTM_HEDGE * strike_int}PE",
    }


# --------------------------- simulator ---------------------------
def simulate_day(fy, idx: str, day: dt.date) -> dict[str, Any]:
    cfg = INDICES[idx]

    # 1. underlying 5-min today
    udf = fetch_5min(fy, cfg["underlying"], day)
    if udf is None:
        return {"error": f"no underlying data for {day} (holiday?)"}

    entry_bar = udf[udf["t"] == ENTRY_T]
    if len(entry_bar) == 0:
        return {"error": "no 10:00 candle on underlying"}
    spot_at_entry = float(entry_bar.iloc[0]["o"])

    # 2. ATM rounded to strike interval
    atm = int(round(spot_at_entry / cfg["strike_int"]) * cfg["strike_int"])

    # 3. expiry
    expiry = get_nearest_expiry(fy, idx)
    if not expiry:
        return {"error": "could not fetch nearest expiry"}

    # 4. build 4 option symbols
    syms = build_symbols(cfg["prefix"], expiry, atm, cfg["strike_int"])

    # 5. fetch 5-min intraday for each leg (with retries; Fyers rate-limits
    #    rapid back-to-back calls)
    leg_df: dict[str, pd.DataFrame] = {}
    for name, sym in syms.items():
        df = None
        for attempt in range(3):
            df = fetch_5min(fy, sym, day)
            if df is not None:
                break
            time.sleep(0.6 * (attempt + 1))
        if df is None:
            return {"error": f"no data for leg {name} ({sym}) after 3 retries"}
        leg_df[name] = df
        time.sleep(0.25)   # space out subsequent calls

    # 6. entry premium = open of 10:00 5-min candle
    entries: dict[str, float] = {}
    for name, df in leg_df.items():
        bar = df[df["t"] == ENTRY_T]
        if len(bar) == 0:
            return {"error": f"no 10:00 candle for {name}"}
        entries[name] = float(bar.iloc[0]["o"])

    # 7. simulate intraday SL/re-entry on sell legs
    events: dict[str, list[tuple[str, str, float]]] = {n: [] for n in syms}
    state: dict[str, str] = {
        "sell_ce": "SHORT", "sell_pe": "SHORT",
        "buy_ce":  "LONG",  "buy_pe":  "LONG",
    }

    for name, st in state.items():
        action = "SELL_OPEN" if st == "SHORT" else "BUY_OPEN"
        events[name].append((ENTRY_T, action, entries[name]))

    # walk by union of timestamps after entry, up to and including exit
    all_t = sorted({t for df in leg_df.values() for t in df["t"]
                    if ENTRY_T < t <= EXIT_T})

    for t in all_t:
        for name in ("sell_ce", "sell_pe"):
            bar_row = leg_df[name][leg_df[name]["t"] == t]
            if len(bar_row) == 0:
                continue
            b = bar_row.iloc[0]
            entry_p = entries[name]
            sl_lvl = entry_p * (1 + SL_PCT)
            if state[name] == "SHORT":
                if float(b["h"]) >= sl_lvl:
                    # SL hit: buy back at sl_lvl
                    events[name].append((t, "BUY_SL", sl_lvl))
                    state[name] = "SL_OUT"
            elif state[name] == "SL_OUT":
                # re-enter when premium returns to original entry
                if float(b["l"]) <= entry_p:
                    events[name].append((t, "SELL_REOPEN", entry_p))
                    state[name] = "SHORT"

    # 8. close at EXIT_T close (or last available bar if EXIT_T missing)
    exit_prems: dict[str, float] = {}
    for name, df in leg_df.items():
        bar = df[df["t"] == EXIT_T]
        if len(bar) == 0:
            bar = df.tail(1)
        exit_prems[name] = float(bar.iloc[0]["c"])

    for name, st in state.items():
        if st == "SHORT":
            events[name].append((EXIT_T, "BUY_CLOSE", exit_prems[name]))
            state[name] = "FLAT"
        elif st == "LONG":
            events[name].append((EXIT_T, "SELL_CLOSE", exit_prems[name]))
            state[name] = "FLAT"
        # SL_OUT already closed via BUY_SL

    # 9. compute gross P&L per leg (cash basis)
    qty = cfg["lot"] * LOTS
    leg_pnl: dict[str, float] = {}
    for name, evts in events.items():
        cash = 0.0
        for (_, action, price) in evts:
            if action.startswith("SELL"):
                cash += price * qty
            else:
                cash -= price * qty
        leg_pnl[name] = cash
    gross = sum(leg_pnl.values())

    # 10. charges
    charges = compute_charges(events, cfg, LOTS)
    net = gross - charges["total"]

    # 11. max-loss & margin estimate (defined-risk IC)
    max_loss_per_lot = (OTM_HEDGE - OTM_SELL) * cfg["strike_int"] * cfg["lot"]
    max_loss_total = max_loss_per_lot * LOTS
    margin_est = max_loss_total * 1.5   # rough multiplier for SPAN + exposure

    return {
        "index": idx, "date": day.isoformat(), "expiry": expiry,
        "spot_at_entry": spot_at_entry, "atm": atm,
        "strikes": {
            "sell_ce": atm + OTM_SELL * cfg["strike_int"],
            "sell_pe": atm - OTM_SELL * cfg["strike_int"],
            "buy_ce":  atm + OTM_HEDGE * cfg["strike_int"],
            "buy_pe":  atm - OTM_HEDGE * cfg["strike_int"],
        },
        "symbols": syms,
        "entries": entries, "exit_prems": exit_prems,
        "events": events, "leg_pnl": leg_pnl,
        "gross_pnl": gross, "charges": charges, "net_pnl": net,
        "sl_count": sum(1 for evts in events.values()
                        for (_,a,_) in evts if "_SL" in a),
        "reentry_count": sum(1 for evts in events.values()
                             for (_,a,_) in evts if "REOPEN" in a),
        "lots": LOTS, "lot_size": cfg["lot"],
        "max_loss_per_lot": max_loss_per_lot,
        "max_loss_total":   max_loss_total,
        "margin_est":       margin_est,
    }


def compute_charges(events: dict, cfg: dict, lots: int) -> dict:
    qty = cfg["lot"] * lots
    exch = cfg["exchange"]
    sell_turnover = 0.0
    buy_turnover  = 0.0
    n_orders = 0
    for evts in events.values():
        for (_, action, price) in evts:
            n_orders += 1
            turnover = price * qty
            if action.startswith("SELL"):
                sell_turnover += turnover
            else:
                buy_turnover += turnover
    total_t = sell_turnover + buy_turnover
    brokerage = n_orders * BROKERAGE_PER_ORDER
    stt = sell_turnover * STT_SELL_RATE
    txn = total_t * TXN_RATE[exch]
    sebi = total_t * SEBI_RATE
    stamp = buy_turnover * STAMP_BUY_RATE
    gst = (brokerage + txn + sebi) * GST_RATE
    total = brokerage + stt + txn + sebi + stamp + gst
    return {
        "n_orders": n_orders,
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "txn": round(txn, 2),
        "sebi": round(sebi, 4),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


# --------------------------- persistence ---------------------------
def append_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    df = pd.DataFrame(rows)
    if path.exists():
        df.to_csv(path, mode="a", header=False, index=False)
    else:
        df.to_csv(path, index=False)


# --------------------------- slack ---------------------------
def build_blocks(date_iso: str, results: dict, cum_stats: dict) -> list[dict]:
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"📊 Iron Condor Forward Test — {date_iso}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "_5/9 strike width · 10:00 entry · 15:00 exit · 35% SL with re-entry "
                 "· internal use only_"}]},
    ]
    total_net = 0.0
    for idx, r in results.items():
        if "error" in r:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
             "text": f"*{idx}*: :grey_question: {r['error']}"}})
            blocks.append({"type": "divider"})
            continue
        emoji = "🟢" if r["net_pnl"] >= 0 else "🔴"
        total_net += r["net_pnl"]
        text = (
            f"{emoji} *{idx}* — spot@10:00 *{r['spot_at_entry']:,.0f}* · "
            f"ATM *{r['atm']:,}* · expiry {r['expiry']}\n"
            f"   sell: {r['strikes']['sell_ce']}CE @ {r['entries']['sell_ce']:.2f} · "
            f"{r['strikes']['sell_pe']}PE @ {r['entries']['sell_pe']:.2f}\n"
            f"   hedge: {r['strikes']['buy_ce']}CE @ {r['entries']['buy_ce']:.2f} · "
            f"{r['strikes']['buy_pe']}PE @ {r['entries']['buy_pe']:.2f}\n"
            f"   exit: CE {r['exit_prems']['sell_ce']:.2f}/{r['exit_prems']['buy_ce']:.2f} · "
            f"PE {r['exit_prems']['sell_pe']:.2f}/{r['exit_prems']['buy_pe']:.2f}\n"
            f"   SL hits: *{r['sl_count']}* · re-entries: *{r['reentry_count']}*\n"
            f"   gross ₹{r['gross_pnl']:,.0f}  −  charges ₹{r['charges']['total']:,.0f} "
            f"(B{r['charges']['brokerage']:.0f}/STT{r['charges']['stt']:.0f}/"
            f"TXN{r['charges']['txn']:.0f}/GST{r['charges']['gst']:.0f}/"
            f"ST{r['charges']['stamp']:.1f})\n"
            f"   *net P&L: ₹{r['net_pnl']:,.0f}*  ({r['lots']} lot{'s' if r['lots']>1 else ''}, "
            f"max loss ₹{r['max_loss_total']:,.0f})"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})

    arrow_total = "🟢" if total_net >= 0 else "🔴"
    foot = (f"{arrow_total} *Today total: ₹{total_net:,.0f}*\n"
            f"_Cumulative since inception ({cum_stats.get('first_date','—')}): "
            f"₹{cum_stats.get('cum_net',0):,.0f} over "
            f"{cum_stats.get('n_days',0)} day(s) · "
            f"{cum_stats.get('n_trades',0)} index-trades · "
            f"win {cum_stats.get('win_pct',0):.0f}%_")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": foot}})
    return blocks


def post_slack(blocks: list[dict]):
    token = os.environ.get("SLACK_BOT_TOKEN")
    ch = os.environ.get("SLACK_CHANNEL")
    if not (token and ch):
        print("No Slack creds — skipping post.", file=sys.stderr)
        return
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"channel": ch, "blocks": blocks,
                            "text": "Iron Condor forward test"}, timeout=30)
    j = r.json() if r.ok else {}
    if j.get("ok"):
        print(f"Posted Slack ts={j.get('ts')}")
    else:
        print(f"Slack failed: {r.status_code} {r.text[:300]}", file=sys.stderr)


# --------------------------- main ---------------------------
def main():
    fy = fy_client()
    # Allow override day for backfill: IC_DAY=YYYY-MM-DD
    if os.environ.get("IC_DAY"):
        day = dt.date.fromisoformat(os.environ["IC_DAY"])
    else:
        day = dt.date.today()
    print(f"Run for {day} (lots={LOTS})")

    results: dict[str, dict] = {}
    daily_rows: list[dict] = []
    trade_rows: list[dict] = []

    for idx in INDICES:
        print(f"\n=== {idx} ===")
        res = simulate_day(fy, idx, day)
        results[idx] = res
        if "error" in res:
            print(f"  error: {res['error']}")
            continue
        print(f"  spot@10:00 {res['spot_at_entry']:.2f} · ATM {res['atm']} · "
              f"expiry {res['expiry']}")
        for n in ("sell_ce","sell_pe","buy_ce","buy_pe"):
            print(f"    {n:8s} K {res['strikes'][n]:>6} "
                  f"entry {res['entries'][n]:>8.2f}  exit {res['exit_prems'][n]:>8.2f}")
        print(f"  SL hits {res['sl_count']} · re-entries {res['reentry_count']}")
        print(f"  gross ₹{res['gross_pnl']:,.2f} · charges ₹{res['charges']['total']:,.2f} "
              f"({res['charges']['n_orders']} orders) · NET ₹{res['net_pnl']:,.2f}")

        daily_rows.append({
            "date": res["date"], "index": idx, "expiry": res["expiry"],
            "spot_at_entry": round(res["spot_at_entry"], 2),
            "atm": res["atm"],
            "sell_ce_K": res["strikes"]["sell_ce"],
            "sell_ce_entry": round(res["entries"]["sell_ce"], 2),
            "sell_ce_exit":  round(res["exit_prems"]["sell_ce"], 2),
            "sell_pe_K": res["strikes"]["sell_pe"],
            "sell_pe_entry": round(res["entries"]["sell_pe"], 2),
            "sell_pe_exit":  round(res["exit_prems"]["sell_pe"], 2),
            "buy_ce_K": res["strikes"]["buy_ce"],
            "buy_ce_entry":  round(res["entries"]["buy_ce"], 2),
            "buy_ce_exit":   round(res["exit_prems"]["buy_ce"], 2),
            "buy_pe_K": res["strikes"]["buy_pe"],
            "buy_pe_entry":  round(res["entries"]["buy_pe"], 2),
            "buy_pe_exit":   round(res["exit_prems"]["buy_pe"], 2),
            "lots": res["lots"], "lot_size": res["lot_size"],
            "sl_count": res["sl_count"], "reentry_count": res["reentry_count"],
            "gross_pnl": round(res["gross_pnl"], 2),
            "brokerage": res["charges"]["brokerage"],
            "stt": res["charges"]["stt"],
            "txn": res["charges"]["txn"],
            "sebi": res["charges"]["sebi"],
            "stamp": res["charges"]["stamp"],
            "gst": res["charges"]["gst"],
            "total_charges": res["charges"]["total"],
            "n_orders": res["charges"]["n_orders"],
            "net_pnl": round(res["net_pnl"], 2),
            "max_loss_total": res["max_loss_total"],
            "margin_est": res["margin_est"],
        })

        for leg, evts in res["events"].items():
            for (t, action, price) in evts:
                trade_rows.append({
                    "date": res["date"], "index": idx,
                    "leg": leg, "strike": res["strikes"][leg],
                    "symbol": res["symbols"][leg],
                    "time": t, "action": action,
                    "price": round(price, 2),
                    "qty": res["lot_size"] * res["lots"],
                })

    append_csv(DAILY_CSV,  daily_rows)
    append_csv(TRADES_CSV, trade_rows)
    print(f"\nWrote {len(daily_rows)} daily row(s), {len(trade_rows)} event(s).")

    # cumulative stats
    cum_stats: dict[str, Any] = {}
    if DAILY_CSV.exists():
        df = pd.read_csv(DAILY_CSV)
        if len(df):
            cum_stats = {
                "first_date": df["date"].min(),
                "n_days": df["date"].nunique(),
                "n_trades": len(df),
                "cum_net":  float(df["net_pnl"].sum()),
                "win_pct":  float((df["net_pnl"] > 0).mean() * 100),
            }
    blocks = build_blocks(day.isoformat(), results, cum_stats)
    post_slack(blocks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
