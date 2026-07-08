"""
Build docs/backtest_data.json for the Iron Condor backtest page from the Volrix
run outputs. Trades come from the two large Volrix trade dumps (saved to disk by
the MCP layer); daily P&L + the equity curve are aggregated from those trades;
headline metrics are embedded from the Volrix metrics() summaries.

One-off historical generation (Volrix free plan = last ~6 months). Re-run with new
dumps + metrics if the window is extended (e.g. after a Max-plan 2-year run).
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "output" / "backtest_data.json"   # this repo deploys Pages from output/

# Volrix trade dumps (session tool-result files)
FILES = {
    "NIFTY":  "/Users/nithin/.claude/projects/-Users-nithin-fyers-bot/f5471e7a-f8b2-4533-bcac-826ccd5986f9/tool-results/mcp-claude_ai_Volrix-trades-1783540555868.txt",
    "SENSEX": "/Users/nithin/.claude/projects/-Users-nithin-fyers-bot/f5471e7a-f8b2-4533-bcac-826ccd5986f9/tool-results/mcp-claude_ai_Volrix-trades-1783540573077.txt",
}

# Headline metrics from Volrix metrics() (capital = per-index margin, 1 lot)
METRICS = {
    "NIFTY": {"period": "2026-01-05 to 2026-07-07", "capital": 22500,
              "totalReturns": 16488.4, "totalTrades": 624, "avgTradesPerDay": 5.1,
              "winRateDaily": 57.7, "winRateTrades": 37.7, "maxDrawdown": -10831.1,
              "sharpe": 1.28, "sortino": 1.96, "profitFactor": 1.1,
              "avgMonthly": 2355.5, "roc": 73.28, "rr": 1.7,
              "avgProfit": 514.3, "avgLoss": -487.8},
    "SENSEX": {"period": "2026-01-06 to 2026-07-07", "capital": 12000,
               "totalReturns": 52372.7, "totalTrades": 605, "avgTradesPerDay": 5.0,
               "winRateDaily": 59.8, "winRateTrades": 39.3, "maxDrawdown": -10336.9,
               "sharpe": 2.94, "sortino": 5.24, "profitFactor": 1.1,
               "avgMonthly": 7481.8, "roc": 436.44, "rr": 1.7,
               "avgProfit": 816.5, "avgLoss": -729.9},
}

# Strike interval per index → to label OTM distance from the leg symbol
STRIKE_INT = {"NIFTY": 50, "SENSEX": 100}


def leg_kind(remark: str) -> str:
    r = (remark or "").lower()
    if "sell_ce" in r: return "Sell CE"
    if "sell_pe" in r: return "Sell PE"
    if "buy_ce" in r:  return "Buy CE (hedge)"
    if "buy_pe" in r:  return "Buy PE (hedge)"
    return remark or ""


def load_trades(path: str) -> list[dict]:
    raw = json.loads(Path(path).read_text())
    return raw["data"]["data"]["trades"]


def compact(t: dict) -> dict:
    md = t.get("metadata", {}) or {}
    return {
        "date": t["entryDate"],
        "et": (t.get("entryTime") or "")[11:19],
        "xt": (t.get("exitTime") or "")[11:19],
        "sym": t.get("symbol", ""),
        "leg": leg_kind(t.get("entryRemarks", "")),
        "side": t.get("transactionType", ""),
        "ep": round(float(t.get("entryPrice", 0)), 2),
        "xp": round(float(t.get("exitPrice", 0)), 2),
        "pnl": round(float(t.get("pnl", 0)), 1),
        "exit": "SL" if "SL" in (t.get("exitRemarks") or "") else "EOD",
        "dte": t.get("DTE", ""),
        "spot": round(float(md.get("entrySpotPrice", 0)), 0),
    }


def curve(trades: list[dict]) -> list[dict]:
    day = defaultdict(float)
    for t in trades:
        day[t["date"]] += t["pnl"]
    cum, out = 0.0, []
    for d in sorted(day):
        cum += day[d]
        out.append({"date": d, "pnl": round(day[d], 1), "cum": round(cum, 1)})
    return out


def main():
    payload = {"strategy": "Iron Condor — 10:00 entry, sell 5-OTM / buy 9-OTM, "
                           "35% SL + re-entry, 15:00 exit, nearest weekly",
               "engine": "Volrix (real historical option data)",
               "lots": 1, "indices": {}}
    for idx, path in FILES.items():
        trades = [compact(t) for t in load_trades(path)]
        payload["indices"][idx] = {
            "metrics": METRICS[idx],
            "curve": curve(trades),
            "trades": trades,
        }
        print(f"{idx}: {len(trades)} trades, "
              f"{len(payload['indices'][idx]['curve'])} days, "
              f"sum pnl {sum(t['pnl'] for t in trades):,.0f}")
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
