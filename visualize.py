"""Generate a polished FII/DII chart and dashboard HTML."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import FancyBboxPatch

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "output"
HISTORY_CSV = DATA_DIR / "fii_dii_history.csv"

NAVY = "#0B2545"
GREEN = "#16A34A"
RED = "#DC2626"
GREY = "#6B7280"
BG = "#FFFFFF"
GRID = "#E5E7EB"


def _fmt_cr(v: float) -> str:
    sign = "−" if v < 0 else "+"
    return f"{sign}₹{abs(v):,.0f} Cr"


def load() -> pd.DataFrame:
    df = pd.read_csv(HISTORY_CSV, parse_dates=["date"])
    df = df.sort_values(["date", "category"])
    return df


def build_chart(df: pd.DataFrame) -> tuple[Path, dict]:
    OUT_DIR.mkdir(exist_ok=True)

    latest_date = df["date"].max()
    latest = df[df["date"] == latest_date].set_index("category")

    fii_key = next((k for k in latest.index if k.upper().startswith("FII")), latest.index[0])
    dii_key = next((k for k in latest.index if k.upper().startswith("DII")), latest.index[-1])
    fii = latest.loc[fii_key]
    dii = latest.loc[dii_key]

    pivot = (
        df.pivot_table(index="date", columns="category", values="net_value", aggfunc="sum")
        .sort_index()
        .tail(30)
    )
    fii_col = next((c for c in pivot.columns if c.upper().startswith("FII")), pivot.columns[0])
    dii_col = next((c for c in pivot.columns if c.upper().startswith("DII")), pivot.columns[-1])

    fig = plt.figure(figsize=(14, 9), facecolor=BG)
    gs = fig.add_gridspec(3, 2, height_ratios=[0.6, 1.4, 1.6], hspace=0.55, wspace=0.25)

    ax_title = fig.add_subplot(gs[0, :])
    ax_title.axis("off")
    ax_title.text(
        0.0, 0.78, "FII / DII ACTIVITY", fontsize=26, fontweight="bold", color=NAVY,
        family="DejaVu Sans",
    )
    ax_title.text(
        0.0, 0.32, f"Cash Market — {latest_date.strftime('%A, %d %B %Y')}",
        fontsize=13, color=GREY,
    )
    ax_title.text(
        1.0, 0.78, "myfinancial.in", fontsize=13, color=NAVY, ha="right", fontweight="bold",
    )
    ax_title.text(
        1.0, 0.32, "Source: NSE", fontsize=10, color=GREY, ha="right",
    )

    ax_fii = fig.add_subplot(gs[1, 0])
    ax_dii = fig.add_subplot(gs[1, 1])

    for ax, row, label in [(ax_fii, fii, "FII / FPI"), (ax_dii, dii, "DII")]:
        ax.set_facecolor(BG)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(left=False, bottom=False, labelleft=False)
        buy = row["buy_value"]
        sell = row["sell_value"]
        net = row["net_value"]
        bars = ax.bar(
            ["Buy", "Sell"], [buy, sell],
            color=[GREEN, RED], width=0.55, edgecolor="none",
        )
        for b, v in zip(bars, [buy, sell]):
            ax.text(
                b.get_x() + b.get_width() / 2, v, f"₹{v:,.0f}",
                ha="center", va="bottom", fontsize=11, color=NAVY, fontweight="bold",
            )
        ax.set_title(label, fontsize=15, color=NAVY, fontweight="bold", loc="left", pad=14)
        net_color = GREEN if net >= 0 else RED
        ax.text(
            0.5, -0.18, f"Net: {_fmt_cr(net)}",
            transform=ax.transAxes, ha="center", fontsize=14,
            color=net_color, fontweight="bold",
        )
        ax.set_xticks([0, 1])
        ax.set_xticklabels(["Buy", "Sell"], fontsize=11, color=GREY)
        ax.set_ylim(0, max(buy, sell) * 1.18)

    ax_tr = fig.add_subplot(gs[2, :])
    ax_tr.set_facecolor(BG)
    x = pivot.index
    width = 0.4
    x_num = mdates.date2num(x)
    ax_tr.bar(
        x_num - width / 2, pivot[fii_col].values, width=width,
        color=[GREEN if v >= 0 else RED for v in pivot[fii_col].values],
        alpha=0.55, label="FII Net", edgecolor="none",
    )
    ax_tr.bar(
        x_num + width / 2, pivot[dii_col].values, width=width,
        color=[NAVY if v >= 0 else "#F59E0B" for v in pivot[dii_col].values],
        alpha=0.85, label="DII Net", edgecolor="none",
    )
    ax_tr.axhline(0, color=GREY, linewidth=0.8)
    ax_tr.set_title("Last 30 sessions — Net flows (₹ Cr)", fontsize=13, color=NAVY,
                    fontweight="bold", loc="left", pad=10)
    ax_tr.xaxis_date()
    ax_tr.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
    ax_tr.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax_tr.tick_params(colors=GREY, labelsize=9)
    for spine in ["top", "right"]:
        ax_tr.spines[spine].set_visible(False)
    ax_tr.spines["left"].set_color(GRID)
    ax_tr.spines["bottom"].set_color(GRID)
    ax_tr.grid(axis="y", color=GRID, linewidth=0.6)
    ax_tr.legend(loc="upper left", frameon=False, fontsize=10)

    fig.text(
        0.5, 0.01,
        "Positive = net buying  •  Negative = net selling  •  Cash segment only",
        ha="center", fontsize=9, color=GREY,
    )

    chart_path = OUT_DIR / "fii_dii_latest.png"
    fig.savefig(chart_path, dpi=160, bbox_inches="tight", facecolor=BG)
    plt.close(fig)

    summary = {
        "date": latest_date.strftime("%Y-%m-%d"),
        "date_pretty": latest_date.strftime("%A, %d %B %Y"),
        "fii": {
            "buy": float(fii["buy_value"]),
            "sell": float(fii["sell_value"]),
            "net": float(fii["net_value"]),
        },
        "dii": {
            "buy": float(dii["buy_value"]),
            "sell": float(dii["sell_value"]),
            "net": float(dii["net_value"]),
        },
    }
    return chart_path, summary


def build_html(summary: dict, df: pd.DataFrame) -> Path:
    tail = (
        df.pivot_table(index="date", columns="category", values="net_value", aggfunc="sum")
        .sort_index(ascending=False)
        .head(15)
    )
    fii_col = next((c for c in tail.columns if c.upper().startswith("FII")), tail.columns[0])
    dii_col = next((c for c in tail.columns if c.upper().startswith("DII")), tail.columns[-1])

    rows_html = "\n".join(
        f"<tr><td>{idx.strftime('%d %b %Y')}</td>"
        f"<td class='{'pos' if tail.loc[idx, fii_col] >= 0 else 'neg'}'>"
        f"{tail.loc[idx, fii_col]:+,.0f}</td>"
        f"<td class='{'pos' if tail.loc[idx, dii_col] >= 0 else 'neg'}'>"
        f"{tail.loc[idx, dii_col]:+,.0f}</td></tr>"
        for idx in tail.index
    )

    fii_net_cls = "pos" if summary["fii"]["net"] >= 0 else "neg"
    dii_net_cls = "pos" if summary["dii"]["net"] >= 0 else "neg"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FII / DII Tracker — {summary['date']}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {{ --navy:#0B2545; --green:#16A34A; --red:#DC2626; --grey:#6B7280; --bg:#F8FAFC; --card:#FFFFFF; --grid:#E5E7EB; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--navy); margin:0; padding:32px 20px; }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  header {{ display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:24px; flex-wrap:wrap; gap:12px; }}
  h1 {{ font-size:32px; margin:0; font-weight:800; letter-spacing:-0.02em; }}
  .sub {{ color:var(--grey); font-size:14px; margin-top:4px; }}
  .brand {{ color:var(--navy); font-weight:700; font-size:14px; }}
  .cards {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:24px; }}
  @media (max-width:640px) {{ .cards {{ grid-template-columns:1fr; }} }}
  .card {{ background:var(--card); border-radius:16px; padding:22px; box-shadow:0 1px 3px rgba(0,0,0,0.05); border:1px solid var(--grid); }}
  .card h2 {{ margin:0 0 14px; font-size:14px; letter-spacing:0.08em; color:var(--grey); text-transform:uppercase; font-weight:600; }}
  .row {{ display:flex; justify-content:space-between; font-size:15px; padding:6px 0; }}
  .row span:last-child {{ font-weight:600; }}
  .net {{ font-size:26px; font-weight:800; margin-top:12px; }}
  .pos {{ color:var(--green); }} .neg {{ color:var(--red); }}
  .chart {{ background:var(--card); border-radius:16px; padding:14px; border:1px solid var(--grid); margin-bottom:24px; }}
  .chart img {{ width:100%; height:auto; display:block; border-radius:10px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border-radius:16px; overflow:hidden; border:1px solid var(--grid); }}
  th,td {{ padding:12px 16px; text-align:right; font-size:14px; }}
  th:first-child,td:first-child {{ text-align:left; }}
  th {{ background:#F1F5F9; color:var(--grey); font-weight:600; font-size:12px; letter-spacing:0.05em; text-transform:uppercase; }}
  tr+tr td {{ border-top:1px solid var(--grid); }}
  footer {{ color:var(--grey); font-size:12px; text-align:center; margin-top:28px; }}
  footer a {{ color:var(--navy); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>FII / DII Activity</h1>
      <div class="sub">Cash market • {summary['date_pretty']} • Source: NSE</div>
    </div>
    <div class="brand">myfinancial.in</div>
  </header>

  <div class="cards">
    <div class="card">
      <h2>FII / FPI</h2>
      <div class="row"><span>Buy</span><span>₹{summary['fii']['buy']:,.0f} Cr</span></div>
      <div class="row"><span>Sell</span><span>₹{summary['fii']['sell']:,.0f} Cr</span></div>
      <div class="net {fii_net_cls}">Net {'+' if summary['fii']['net']>=0 else '−'}₹{abs(summary['fii']['net']):,.0f} Cr</div>
    </div>
    <div class="card">
      <h2>DII</h2>
      <div class="row"><span>Buy</span><span>₹{summary['dii']['buy']:,.0f} Cr</span></div>
      <div class="row"><span>Sell</span><span>₹{summary['dii']['sell']:,.0f} Cr</span></div>
      <div class="net {dii_net_cls}">Net {'+' if summary['dii']['net']>=0 else '−'}₹{abs(summary['dii']['net']):,.0f} Cr</div>
    </div>
  </div>

  <div class="chart">
    <img src="fii_dii_latest.png" alt="FII DII chart">
  </div>

  <table>
    <thead><tr><th>Date</th><th>FII Net (₹ Cr)</th><th>DII Net (₹ Cr)</th></tr></thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <footer>
    Auto-generated daily • Built by <a href="https://myfinancial.in">myfinancial.in</a> •
    <a href="https://www.nseindia.com/reports/fii-dii">NSE source</a>
  </footer>
</div>
</body>
</html>
"""
    out = OUT_DIR / "index.html"
    out.write_text(html)
    return out


def main() -> int:
    df = load()
    chart, summary = build_chart(df)
    html = build_html(summary, df)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Wrote {chart}")
    print(f"Wrote {html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
