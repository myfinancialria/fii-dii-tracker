"""
Render the three strategy workflows' latest results as an HTML block for the
dashboard: tomorrow's range forecast, the Iron Condor forward test, and the
pairs-trading book. Reads the committed data files each workflow produces, so it
works from any workflow that rebuilds the page.

Fully defensive — any missing/short file degrades to a "no data yet" note and
never breaks the main FII/DII page. `render()` returns an HTML string.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
FORECAST_DIR = HERE / "data" / "forecasts"
IC_CSV = HERE / "iron_condor_daily.csv"
PAIRS_POS = HERE / "pairs_positions.json"
PAIRS_EQ = HERE / "pairs_equity_daily.csv"
PAIRS_CLOSED = HERE / "pairs_closed_trades.csv"


def _sign(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _rupee(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}₹{abs(v):,.0f}"


def _card(title: str, asof: str, body: str) -> str:
    tag = f"<span class='strat-asof'>{asof}</span>" if asof else ""
    return (f"<div class='mover strat-card'><div class='strat-head'>"
            f"<h3>{title}</h3>{tag}</div>{body}</div>")


# ---------------- forecast ----------------
def _forecast() -> str:
    files = sorted(FORECAST_DIR.glob("*.json")) if FORECAST_DIR.exists() else []
    if not files:
        return _card("Tomorrow's range forecast", "", "<p class='strat-none'>No forecast yet.</p>")
    d = json.loads(files[-1].read_text())
    rows = []
    for name, s in (d.get("symbols") or {}).items():
        nb = (s.get("tomorrow") or {}).get("normal") or {}
        lo, hi = nb.get("low"), nb.get("high")
        bias = s.get("bias", "")
        bcls = "pos" if bias == "bullish" else "neg" if bias == "bearish" else "grey"
        piv = s.get("pivot") or {}
        rng = f"{lo:,.0f} – {hi:,.0f}" if lo and hi else "—"
        rows.append(
            f"<tr><td>{name}</td>"
            f"<td>{s.get('close', 0):,.0f}</td>"
            f"<td class='{bcls}'>{bias or '—'}</td>"
            f"<td>{rng}</td>"
            f"<td>{piv.get('S1', 0):,.0f} / {piv.get('R1', 0):,.0f}</td></tr>")
    body = (f"<table><thead><tr><th>Index</th><th>Close</th><th>Bias</th>"
            f"<th>Expected range</th><th>S1 / R1</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")
    return _card(f"Range forecast for {d.get('forecast_for', '')}",
                 f"as of {d.get('date', '')}", body)


# ---------------- iron condor ----------------
def _iron_condor() -> str:
    if not IC_CSV.exists():
        return _card("Iron Condor forward test", "", "<p class='strat-none'>No trades yet.</p>")
    df = pd.read_csv(IC_CSV)
    if df.empty:
        return _card("Iron Condor forward test", "", "<p class='strat-none'>No trades yet.</p>")
    cum = df["net_pnl"].sum()
    last_day = df["date"].iloc[-1]
    today = df[df["date"] == last_day]
    day_pnl = today["net_pnl"].sum()
    n_days = df["date"].nunique()
    wins = (df.groupby("date")["net_pnl"].sum() > 0).sum()
    wr = wins / n_days * 100 if n_days else 0
    rows = "".join(
        f"<tr><td>{r['index']}</td><td>{int(r['sell_pe_K'])}–{int(r['sell_ce_K'])}</td>"
        f"<td class='{_sign(r['net_pnl'])}'>{_rupee(r['net_pnl'])}</td></tr>"
        for _, r in today.iterrows())
    body = (f"<div class='strat-kpis'>"
            f"<div><span>Cumulative</span><b class='{_sign(cum)}'>{_rupee(cum)}</b></div>"
            f"<div><span>{last_day}</span><b class='{_sign(day_pnl)}'>{_rupee(day_pnl)}</b></div>"
            f"<div><span>Win days</span><b>{wins}/{n_days} ({wr:.0f}%)</b></div></div>"
            f"<table><thead><tr><th>Index</th><th>Short strikes</th><th>Net P&amp;L</th></tr>"
            f"</thead><tbody>{rows}</tbody></table>"
            f"<a href='backtest.html' style='display:inline-block;margin-top:10px;font-size:12.5px;"
            f"font-weight:600'>View 6-month backtest (trade log + equity curve) →</a>")
    return _card("Iron Condor forward test", f"as of {last_day}", body)


# ---------------- pairs ----------------
def _pairs() -> str:
    open_trades, asof = [], ""
    if PAIRS_POS.exists():
        try:
            open_trades = (json.loads(PAIRS_POS.read_text()) or {}).get("open", [])
        except Exception:
            open_trades = []
    cum_real = open_mtm = None
    n_closed = 0
    if PAIRS_EQ.exists():
        eq = pd.read_csv(PAIRS_EQ)
        if len(eq):
            last = eq.iloc[-1]
            asof = str(last["date"])
            cum_real = float(last.get("cum_realized_pct", 0))
            open_mtm = float(last.get("open_mtm_pct", 0))
    if PAIRS_CLOSED.exists():
        try:
            n_closed = max(0, len(pd.read_csv(PAIRS_CLOSED)))
        except Exception:
            n_closed = 0
    if not open_trades and cum_real is None:
        return _card("Pairs trading book", "", "<p class='strat-none'>No positions yet.</p>")

    kpis = "<div class='strat-kpis'>"
    if cum_real is not None:
        kpis += f"<div><span>Realised</span><b class='{_sign(cum_real)}'>{cum_real:+.2f}%</b></div>"
    if open_mtm is not None:
        kpis += f"<div><span>Open MTM</span><b class='{_sign(open_mtm)}'>{open_mtm:+.2f}%</b></div>"
    kpis += (f"<div><span>Open / Closed</span><b>{len(open_trades)} / {n_closed}</b></div></div>")

    rows = "".join(
        f"<tr><td>{t.get('key', '')}</td>"
        f"<td>{'Long spread' if t.get('direction', 0) > 0 else 'Short spread'}</td>"
        f"<td>{t.get('entry_z', 0):+.2f}</td>"
        f"<td>{t.get('entry_date', '')}</td></tr>"
        for t in open_trades[:6]) or "<tr><td colspan='4' class='strat-none'>No open pairs.</td></tr>"
    body = (kpis + f"<table><thead><tr><th>Pair</th><th>Side</th><th>Entry z</th>"
            f"<th>Since</th></tr></thead><tbody>{rows}</tbody></table>")
    return _card("Pairs trading book", f"as of {asof}", body)


CSS = """
  .strat { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
  @media (max-width:900px) { .strat { grid-template-columns:1fr; } }
  .strat-card h3 { margin:0; font-size:15px; }
  .strat-head { display:flex; justify-content:space-between; align-items:baseline;
    margin-bottom:8px; gap:8px; }
  .strat-asof { color:var(--grey); font-size:11px; white-space:nowrap; }
  .strat-none { color:var(--grey); font-size:13px; margin:6px 0; }
  .strat-kpis { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:10px; }
  .strat-kpis span { display:block; color:var(--grey); font-size:11px;
    text-transform:uppercase; }
  .strat-kpis b { font-size:16px; }
"""


def render() -> str:
    """Full strategies section (title + grid). Never raises."""
    try:
        cards = _forecast() + _iron_condor() + _pairs()
    except Exception as e:  # keep the main page alive no matter what
        return f"<!-- strategies panel error: {e} -->"
    return (f"<div class='section-title'>Strategy trackers</div>"
            f"<div class='strat'>{cards}</div>")


if __name__ == "__main__":
    print(render())
