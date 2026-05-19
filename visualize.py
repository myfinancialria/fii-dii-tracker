"""Render the daily market dashboard: a polished PNG + a rich HTML page."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.rcParams["text.parse_math"] = False

import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "output"
HISTORY_CSV = DATA_DIR / "fii_dii_history.csv"
SNAPSHOT_JSON = DATA_DIR / "snapshot.json"

NAVY = "#0B2545"
GREEN = "#16A34A"
RED = "#DC2626"
AMBER = "#F59E0B"
GREY = "#6B7280"
BG = "#FFFFFF"
GRID = "#E5E7EB"
SOFT = "#F8FAFC"

CHART_SECTORS = [
    "NIFTY IT",
    "NIFTY BANK",
    "NIFTY AUTO",
    "NIFTY PHARMA",
    "NIFTY FMCG",
    "NIFTY METAL",
    "NIFTY REALTY",
    "NIFTY ENERGY",
    "NIFTY PSU BANK",
    "NIFTY FINANCIAL SERVICES",
]

HERO_INDICES = ["NIFTY 50", "NIFTY BANK", "NIFTY MIDCAP 100", "INDIA VIX"]

DISPLAY_NAMES = {
    "NIFTY 50": "Nifty 50",
    "NIFTY BANK": "Bank Nifty",
    "NIFTY MIDCAP 100": "Midcap 100",
    "NIFTY SMALLCAP 100": "Smallcap 100",
    "NIFTY IT": "IT",
    "NIFTY AUTO": "Auto",
    "NIFTY PHARMA": "Pharma",
    "NIFTY FMCG": "FMCG",
    "NIFTY METAL": "Metal",
    "NIFTY REALTY": "Realty",
    "NIFTY ENERGY": "Energy",
    "NIFTY PSU BANK": "PSU Bank",
    "NIFTY FINANCIAL SERVICES": "Financials",
    "INDIA VIX": "India VIX",
}


def _disp(name: str) -> str:
    return DISPLAY_NAMES.get(name, name.replace("NIFTY ", "").title())


def _fmt_pct(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _fmt_cr(v: float) -> str:
    sign = "−" if v < 0 else "+"
    return f"{sign}₹{abs(v):,.0f} Cr"


def _color(v: float) -> str:
    if v > 0:
        return GREEN
    if v < 0:
        return RED
    return GREY


def load_snapshot() -> dict:
    return json.loads(SNAPSHOT_JSON.read_text())


def load_history() -> pd.DataFrame:
    df = pd.read_csv(HISTORY_CSV, parse_dates=["date"])
    return df.sort_values(["date", "category"])


def build_chart(snap: dict, hist: pd.DataFrame) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    date_str = snap.get("date", "")
    date_pretty = pd.to_datetime(date_str).strftime("%A, %d %B %Y") if date_str else ""

    fig = plt.figure(figsize=(15, 17), facecolor=BG)
    gs = fig.add_gridspec(
        6, 4,
        height_ratios=[0.5, 1.1, 2.0, 1.4, 1.6, 0.55],
        hspace=0.7, wspace=0.30,
    )

    ax_t = fig.add_subplot(gs[0, :])
    ax_t.axis("off")
    ax_t.text(0.0, 0.6, "INDIAN MARKET PULSE", fontsize=28, fontweight="bold",
              color=NAVY)
    ax_t.text(0.0, 0.05, f"Cash + indices snapshot  •  {date_pretty}",
              fontsize=13, color=GREY)
    ax_t.text(1.0, 0.6, "myfinancial.in", fontsize=14, color=NAVY,
              fontweight="bold", ha="right")
    mood = snap.get("mood", "")
    mood_color = {"Risk-On": GREEN, "Risk-Off": RED, "Mixed": AMBER,
                  "Range-bound": GREY}.get(mood, GREY)
    ax_t.text(1.0, 0.05, f"Mood: {mood}", fontsize=12, color=mood_color,
              fontweight="bold", ha="right")

    indices = snap.get("indices", {})
    for i, name in enumerate(HERO_INDICES):
        ax = fig.add_subplot(gs[1, i])
        d = indices.get(name, {})
        last = d.get("last", 0)
        pct = d.get("pct", 0)
        chg = d.get("change", 0)
        col = _color(pct)
        ax.set_facecolor(SOFT)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        rect = mpatches.FancyBboxPatch(
            (0.02, 0.05), 0.96, 0.9, boxstyle="round,pad=0.02,rounding_size=0.04",
            transform=ax.transAxes, facecolor=SOFT, edgecolor=GRID, linewidth=1,
        )
        ax.add_patch(rect)
        ax.text(0.5, 0.82, _disp(name),
                fontsize=11, color=GREY, ha="center", transform=ax.transAxes,
                fontweight="600")
        ax.text(0.5, 0.5, f"{last:,.2f}", fontsize=22, color=NAVY,
                fontweight="bold", ha="center", transform=ax.transAxes)
        ax.text(0.5, 0.18, f"{chg:+,.2f}  ({_fmt_pct(pct)})", fontsize=12,
                color=col, fontweight="bold", ha="center", transform=ax.transAxes)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

    ax_s = fig.add_subplot(gs[2, :])
    ax_s.set_facecolor(BG)
    ax_s.set_title("Sector performance (today)", loc="left",
                   fontsize=14, color=NAVY, fontweight="bold", pad=10)
    for spine in ax_s.spines.values():
        spine.set_visible(False)
    sectors = [(_disp(n), indices.get(n, {}).get("pct", 0))
               for n in CHART_SECTORS if indices.get(n)]
    sectors.sort(key=lambda x: x[1], reverse=True)
    names = [s[0] for s in sectors]
    vals = [s[1] for s in sectors]
    colors = [GREEN if v >= 0 else RED for v in vals]
    bars = ax_s.barh(names, vals, color=colors, edgecolor="none", height=0.65)
    ax_s.axvline(0, color=GREY, linewidth=0.6)
    ax_s.invert_yaxis()
    ax_s.tick_params(left=False, bottom=False, labelsize=11, colors=NAVY, pad=8)
    for spine in ["top", "right", "bottom"]:
        ax_s.spines[spine].set_visible(False)
    ax_s.spines["left"].set_color(GRID)
    for b, v in zip(bars, vals):
        x = v + (0.05 if v >= 0 else -0.05)
        ha = "left" if v >= 0 else "right"
        ax_s.text(x, b.get_y() + b.get_height() / 2, _fmt_pct(v),
                  va="center", ha=ha, fontsize=9, color=NAVY, fontweight="600")
    max_abs = max((abs(v) for v in vals), default=1) * 1.25
    ax_s.set_xlim(-max_abs, max_abs)
    ax_s.set_xticks([])

    fii_dii = snap.get("fii_dii", {})
    rows = {r["category"]: r for r in fii_dii.get("rows", [])}
    fii = rows.get("FII/FPI") or next((v for k, v in rows.items()
                                       if k.upper().startswith("FII")), {})
    dii = rows.get("DII") or next((v for k, v in rows.items()
                                   if k.upper().startswith("DII")), {})

    for i, (label, r) in enumerate([("FII / FPI (Cash)", fii), ("DII (Cash)", dii)]):
        ax = fig.add_subplot(gs[3, i * 2:(i + 1) * 2])
        ax.set_facecolor(BG)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(left=False, bottom=False, labelleft=False)
        buy = r.get("buy_value", 0)
        sell = r.get("sell_value", 0)
        net = r.get("net_value", 0)
        bars = ax.bar(["Buy", "Sell"], [buy, sell], color=[GREEN, RED],
                      width=0.5, edgecolor="none")
        for b, v in zip(bars, [buy, sell]):
            ax.text(b.get_x() + b.get_width() / 2, v, f"₹{v:,.0f}",
                    ha="center", va="bottom", fontsize=10,
                    color=NAVY, fontweight="bold")
        ax.set_title(label, fontsize=13, color=NAVY, fontweight="bold",
                     loc="left", pad=10)
        ax.text(0.5, -0.22, f"Net: {_fmt_cr(net)}", transform=ax.transAxes,
                ha="center", fontsize=13, color=_color(net), fontweight="bold")
        ax.set_xticks([0, 1])
        ax.set_xticklabels(["Buy", "Sell"], fontsize=10, color=GREY)
        ax.set_ylim(0, max(buy, sell, 1) * 1.18)

    ax_tr = fig.add_subplot(gs[4, :])
    ax_tr.set_facecolor(BG)
    pivot = (hist.pivot_table(index="date", columns="category",
                              values="net_value", aggfunc="sum")
             .sort_index().tail(30))
    fii_col = next((c for c in pivot.columns if c.upper().startswith("FII")),
                   pivot.columns[0])
    dii_col = next((c for c in pivot.columns if c.upper().startswith("DII")),
                   pivot.columns[-1])
    x_num = mdates.date2num(pivot.index)
    width = 0.4
    ax_tr.bar(x_num - width / 2, pivot[fii_col].values, width=width,
              color=[GREEN if v >= 0 else RED for v in pivot[fii_col].values],
              alpha=0.65, label="FII Net", edgecolor="none")
    ax_tr.bar(x_num + width / 2, pivot[dii_col].values, width=width,
              color=[NAVY if v >= 0 else AMBER for v in pivot[dii_col].values],
              alpha=0.85, label="DII Net", edgecolor="none")
    ax_tr.axhline(0, color=GREY, linewidth=0.8)
    ax_tr.set_title("Last 30 sessions — FII vs DII net flows (₹ Cr)",
                    fontsize=13, color=NAVY, fontweight="bold",
                    loc="left", pad=10)
    ax_tr.xaxis_date()
    ax_tr.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
    ax_tr.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax_tr.tick_params(colors=GREY, labelsize=9)
    for sp in ["top", "right"]:
        ax_tr.spines[sp].set_visible(False)
    ax_tr.spines["left"].set_color(GRID)
    ax_tr.spines["bottom"].set_color(GRID)
    ax_tr.grid(axis="y", color=GRID, linewidth=0.5)
    ax_tr.legend(loc="upper left", frameon=False, fontsize=10)

    ax_g = fig.add_subplot(gs[5, :])
    ax_g.axis("off")
    g = snap.get("global", {})
    fx = snap.get("fx_commodities", {})

    def _short(name: str, d: dict) -> str:
        if not d:
            return f"{name}: —"
        return f"{name} {_fmt_pct(d.get('pct', 0))}"

    line1 = "  •  ".join([
        _short("Dow", g.get("Dow Jones", {})),
        _short("Nasdaq", g.get("Nasdaq", {})),
        _short("S&P", g.get("S&P 500", {})),
        _short("Nikkei", g.get("Nikkei 225", {})),
        _short("HSI", g.get("Hang Seng", {})),
    ])
    usdinr = fx.get("USD/INR", {})
    brent = fx.get("Brent Crude", {})
    gold = fx.get("Gold", {})
    line2 = "  •  ".join([
        f"USD/INR {usdinr.get('last', 0):.2f} ({_fmt_pct(usdinr.get('pct', 0))})"
        if usdinr else "USD/INR —",
        f"Brent ${brent.get('last', 0):.1f} ({_fmt_pct(brent.get('pct', 0))})"
        if brent else "Brent —",
        f"Gold ${gold.get('last', 0):,.0f} ({_fmt_pct(gold.get('pct', 0))})"
        if gold else "Gold —",
    ])
    ax_g.text(0.5, 0.85, "Global cues & FX/commodities", ha="center",
              fontsize=11, color=GREY, fontweight="bold")
    ax_g.text(0.5, 0.50, line1, ha="center", fontsize=11, color=NAVY)
    ax_g.text(0.5, 0.15, line2, ha="center", fontsize=11, color=NAVY)

    chart = OUT_DIR / "market_pulse.png"
    fig.savefig(chart, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    # Backwards-compat name for Slack image
    (OUT_DIR / "fii_dii_latest.png").write_bytes(chart.read_bytes())
    return chart


def _idx_card(name: str, d: dict | None) -> str:
    if not d:
        return ""
    pct = d.get("pct", 0)
    cls = "pos" if pct >= 0 else "neg"
    title = _disp(name)
    yh = d.get("year_high", 0)
    yl = d.get("year_low", 0)
    range_pct = ((d.get("last", 0) - yl) / (yh - yl) * 100) if yh > yl else 0
    return f"""
    <div class="icard">
      <div class="ic-name">{title}</div>
      <div class="ic-val">{d.get('last', 0):,.2f}</div>
      <div class="ic-chg {cls}">{d.get('change', 0):+,.2f} ({d.get('pct', 0):+.2f}%)</div>
      <div class="ic-bar"><div class="ic-bar-fill" style="width:{range_pct:.1f}%"></div></div>
      <div class="ic-rng">52w {yl:,.0f} ─ {yh:,.0f}</div>
    </div>
    """


def _movers_table(title: str, rows: list[dict], cls: str) -> str:
    if not rows:
        return ""
    tr = "\n".join(
        f"<tr><td>{r['symbol']}</td><td>{r['ltp']:,.2f}</td>"
        f"<td class='{cls}'>{r['pct']:+.2f}%</td></tr>"
        for r in rows
    )
    return f"""
    <div class="mover">
      <h3>{title}</h3>
      <table>
        <thead><tr><th>Symbol</th><th>LTP</th><th>Change</th></tr></thead>
        <tbody>{tr}</tbody>
      </table>
    </div>
    """


def _sectors_table(title: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    from_disp = {
        "NIFTY IT": "IT", "NIFTY BANK": "Bank Nifty", "NIFTY AUTO": "Auto",
        "NIFTY PHARMA": "Pharma", "NIFTY FMCG": "FMCG", "NIFTY METAL": "Metal",
        "NIFTY REALTY": "Realty", "NIFTY ENERGY": "Energy",
        "NIFTY PSU BANK": "PSU Bank", "NIFTY FINANCIAL SERVICES": "Financials",
    }
    def _row(r: dict) -> str:
        name = from_disp.get(r["name"], r["name"].replace("NIFTY ", "").title())
        pct = r.get("pct") or 0
        cls = "pos" if pct >= 0 else "neg"
        return (f"<tr><td>{name}</td><td class='{cls}'>{pct:+.2f}%</td></tr>")
    tr = "\n".join(_row(r) for r in rows)
    return f"""
    <div class="mover">
      <h3>{title}</h3>
      <table>
        <thead><tr><th>Sector</th><th>Change</th></tr></thead>
        <tbody>{tr}</tbody>
      </table>
    </div>
    """


def _yf_table(title: str, items: dict[str, dict]) -> str:
    if not items:
        return ""
    tr = "\n".join(
        f"<tr><td>{name}</td><td>{d.get('last', 0):,.2f}</td>"
        f"<td class='{'pos' if d.get('pct', 0) >= 0 else 'neg'}'>{d.get('pct', 0):+.2f}%</td></tr>"
        for name, d in items.items()
    )
    return f"""
    <div class="mover">
      <h3>{title}</h3>
      <table>
        <thead><tr><th>Instrument</th><th>Last</th><th>Change</th></tr></thead>
        <tbody>{tr}</tbody>
      </table>
    </div>
    """


def build_html(snap: dict, hist: pd.DataFrame) -> Path:
    date_str = snap.get("date", "")
    date_pretty = pd.to_datetime(date_str).strftime("%A, %d %B %Y") if date_str else ""
    indices = snap.get("indices", {})

    hero_cards = "\n".join(_idx_card(n, indices.get(n)) for n in HERO_INDICES)
    sector_cards = "\n".join(_idx_card(n, indices.get(n)) for n in CHART_SECTORS
                              if indices.get(n))

    movers = snap.get("movers", {})
    gainers_tbl = _movers_table("Top Gainers (Nifty 50)", movers.get("gainers", []), "pos")
    losers_tbl = _movers_table("Top Losers (Nifty 50)", movers.get("losers", []), "neg")

    cs = snap.get("catalyst_sectors", {}) or {}
    sector_top_tbl = _sectors_table("Best Sectors", cs.get("top", []))
    sector_bot_tbl = _sectors_table("Worst Sectors", cs.get("bottom", []))
    global_tbl = _yf_table("Global Indices", snap.get("global", {}))
    fx_tbl = _yf_table("FX & Commodities", snap.get("fx_commodities", {}))

    fii_dii = snap.get("fii_dii", {})
    rows = {r["category"]: r for r in fii_dii.get("rows", [])}
    fii = rows.get("FII/FPI") or next((v for k, v in rows.items()
                                       if k.upper().startswith("FII")), {})
    dii = rows.get("DII") or next((v for k, v in rows.items()
                                   if k.upper().startswith("DII")), {})

    def _flow_card(label: str, r: dict) -> str:
        net = r.get("net_value", 0)
        cls = "pos" if net >= 0 else "neg"
        return f"""
        <div class="card">
          <h2>{label}</h2>
          <div class="row"><span>Buy</span><span>₹{r.get('buy_value', 0):,.0f} Cr</span></div>
          <div class="row"><span>Sell</span><span>₹{r.get('sell_value', 0):,.0f} Cr</span></div>
          <div class="net {cls}">Net {'+' if net >= 0 else '−'}₹{abs(net):,.0f} Cr</div>
        </div>
        """

    tail = (hist.pivot_table(index="date", columns="category",
                             values="net_value", aggfunc="sum")
            .sort_index(ascending=False).head(10))
    fii_col = next((c for c in tail.columns if c.upper().startswith("FII")), tail.columns[0])
    dii_col = next((c for c in tail.columns if c.upper().startswith("DII")), tail.columns[-1])
    hist_rows = "\n".join(
        f"<tr><td>{idx.strftime('%d %b')}</td>"
        f"<td class='{'pos' if tail.loc[idx, fii_col] >= 0 else 'neg'}'>"
        f"{tail.loc[idx, fii_col]:+,.0f}</td>"
        f"<td class='{'pos' if tail.loc[idx, dii_col] >= 0 else 'neg'}'>"
        f"{tail.loc[idx, dii_col]:+,.0f}</td></tr>"
        for idx in tail.index
    )

    mood = snap.get("mood", "")
    mood_cls = {"Risk-On": "pos", "Risk-Off": "neg",
                "Mixed": "amber", "Range-bound": "grey"}.get(mood, "grey")
    vix = snap.get("vix", {})

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Indian Market Pulse — {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {{ --navy:#0B2545; --green:#16A34A; --red:#DC2626; --amber:#F59E0B; --grey:#6B7280; --bg:#F8FAFC; --card:#FFFFFF; --grid:#E5E7EB; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--navy); margin:0; padding:28px 18px; }}
  .wrap {{ max-width:1180px; margin:0 auto; }}
  header {{ display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:22px; flex-wrap:wrap; gap:12px; }}
  h1 {{ font-size:32px; margin:0; font-weight:800; letter-spacing:-0.02em; }}
  .sub {{ color:var(--grey); font-size:14px; margin-top:4px; }}
  .brand {{ color:var(--navy); font-weight:700; font-size:14px; }}
  .mood {{ display:inline-block; padding:4px 12px; border-radius:999px; font-weight:700; font-size:13px; margin-top:6px; }}
  .mood.pos {{ background:#DCFCE7; color:var(--green); }}
  .mood.neg {{ background:#FEE2E2; color:var(--red); }}
  .mood.amber {{ background:#FEF3C7; color:var(--amber); }}
  .mood.grey {{ background:#F1F5F9; color:var(--grey); }}

  .section-title {{ font-size:13px; letter-spacing:0.1em; color:var(--grey); text-transform:uppercase; font-weight:700; margin:24px 0 10px; }}

  .igrid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }}
  @media (max-width:900px) {{ .igrid {{ grid-template-columns:repeat(2,1fr); }} }}
  .icard {{ background:var(--card); border:1px solid var(--grid); border-radius:14px; padding:16px; }}
  .ic-name {{ font-size:11px; color:var(--grey); letter-spacing:0.08em; text-transform:uppercase; font-weight:600; }}
  .ic-val {{ font-size:22px; font-weight:800; margin:6px 0 2px; }}
  .ic-chg {{ font-size:13px; font-weight:600; }}
  .ic-bar {{ height:4px; background:#E2E8F0; border-radius:99px; margin:10px 0 6px; overflow:hidden; }}
  .ic-bar-fill {{ height:100%; background:linear-gradient(90deg,#DC2626,#F59E0B,#16A34A); }}
  .ic-rng {{ font-size:11px; color:var(--grey); }}

  .flows {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  @media (max-width:640px) {{ .flows {{ grid-template-columns:1fr; }} }}
  .card {{ background:var(--card); border-radius:14px; padding:20px; border:1px solid var(--grid); }}
  .card h2 {{ margin:0 0 12px; font-size:13px; letter-spacing:0.08em; color:var(--grey); text-transform:uppercase; font-weight:700; }}
  .row {{ display:flex; justify-content:space-between; font-size:14px; padding:5px 0; }}
  .row span:last-child {{ font-weight:600; }}
  .net {{ font-size:24px; font-weight:800; margin-top:10px; }}

  .pos {{ color:var(--green); }} .neg {{ color:var(--red); }}

  .chart {{ background:var(--card); border-radius:14px; padding:12px; border:1px solid var(--grid); margin:16px 0; }}
  .chart img {{ width:100%; display:block; border-radius:8px; }}

  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  @media (max-width:860px) {{ .two-col {{ grid-template-columns:1fr; }} }}
  .mover {{ background:var(--card); border:1px solid var(--grid); border-radius:14px; padding:18px; }}
  .mover h3 {{ margin:0 0 10px; font-size:13px; letter-spacing:0.08em; color:var(--grey); text-transform:uppercase; font-weight:700; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:10px 6px; text-align:right; font-size:13px; vertical-align:top; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ color:var(--grey); font-weight:600; font-size:11px; text-transform:uppercase; border-bottom:1px solid var(--grid); }}
  tr+tr td {{ border-top:1px solid var(--grid); }}

  footer {{ color:var(--grey); font-size:12px; text-align:center; margin-top:24px; }}
  footer a {{ color:var(--navy); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Indian Market Pulse</h1>
      <div class="sub">Cash + indices snapshot · {date_pretty} · Source: NSE + Yahoo Finance</div>
      <div class="mood {mood_cls}">Mood: {mood} · India VIX {vix.get('last', 0):.2f} ({vix.get('pct', 0):+.2f}%)</div>
    </div>
    <div class="brand">myfinancial.in</div>
  </header>

  <div class="section-title">Headline indices</div>
  <div class="igrid">{hero_cards}</div>

  <div class="section-title">Sector indices</div>
  <div class="igrid">{sector_cards}</div>

  <div class="section-title">Institutional flow (cash market)</div>
  <div class="flows">
    {_flow_card("FII / FPI", fii)}
    {_flow_card("DII", dii)}
  </div>

  <div class="section-title">Best &amp; Worst sectors today</div>
  <div class="two-col">
    {sector_top_tbl}
    {sector_bot_tbl}
  </div>

  <div class="section-title">Top movers</div>
  <div class="two-col">
    {gainers_tbl}
    {losers_tbl}
  </div>

  <div class="section-title">Global cues</div>
  <div class="two-col">
    {global_tbl}
    {fx_tbl}
  </div>

  <div class="section-title">FII / DII history (last 10 sessions)</div>
  <div class="mover">
    <table>
      <thead><tr><th>Date</th><th>FII Net (₹ Cr)</th><th>DII Net (₹ Cr)</th></tr></thead>
      <tbody>{hist_rows}</tbody>
    </table>
  </div>

  <footer>
    Auto-generated daily · Built by <a href="https://myfinancial.in">myfinancial.in</a> ·
    <a href="https://www.nseindia.com/reports/fii-dii">NSE source</a>
  </footer>
</div>
</body>
</html>
"""
    out = OUT_DIR / "index.html"
    out.write_text(html)
    return out


def build_summary(snap: dict) -> dict:
    """Compact summary dict used by Slack poster."""
    indices = snap.get("indices", {})
    fii_dii = snap.get("fii_dii", {})
    rows = {r["category"]: r for r in fii_dii.get("rows", [])}
    fii = rows.get("FII/FPI") or next((v for k, v in rows.items()
                                       if k.upper().startswith("FII")), {})
    dii = rows.get("DII") or next((v for k, v in rows.items()
                                   if k.upper().startswith("DII")), {})
    date_str = snap.get("date", "")
    date_pretty = pd.to_datetime(date_str).strftime("%A, %d %B %Y") if date_str else ""
    return {
        "date": date_str,
        "date_pretty": date_pretty,
        "mood": snap.get("mood"),
        "nifty": indices.get("NIFTY 50", {}),
        "banknifty": indices.get("NIFTY BANK", {}),
        "sensex_proxy": indices.get("NIFTY 50", {}),
        "midcap": indices.get("NIFTY MIDCAP 100", {}),
        "vix": snap.get("vix", {}),
        "fii": {"buy": fii.get("buy_value", 0),
                "sell": fii.get("sell_value", 0),
                "net": fii.get("net_value", 0)},
        "dii": {"buy": dii.get("buy_value", 0),
                "sell": dii.get("sell_value", 0),
                "net": dii.get("net_value", 0)},
        "global": snap.get("global", {}),
        "fx_commodities": snap.get("fx_commodities", {}),
        "movers": snap.get("movers", {}),
        "catalyst_sectors": snap.get("catalyst_sectors", {}),
    }


def main() -> int:
    snap = load_snapshot()
    hist = load_history()
    chart = build_chart(snap, hist)
    html = build_html(snap, hist)
    summary = build_summary(snap)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"Wrote {chart}")
    print(f"Wrote {html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
