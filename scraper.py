"""Scrape NSE FII/DII + full market snapshot (indices, VIX, breadth, global, FX)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

from fetchers import (
    fetch_breadth,
    fetch_fx_comm,
    fetch_global,
    fetch_indices,
    fetch_movers,
    market_mood,
)

NSE_HOME = "https://www.nseindia.com"
NSE_FII_DII = "https://www.nseindia.com/api/fiidiiTradeReact"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/reports/fii-dii",
}

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
HISTORY_CSV = DATA_DIR / "fii_dii_history.csv"
LATEST_JSON = DATA_DIR / "latest.json"
SNAPSHOT_JSON = DATA_DIR / "snapshot.json"


def fetch_fii_dii() -> list[dict]:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.get(NSE_HOME, timeout=15)
    time.sleep(1)
    sess.get("https://www.nseindia.com/reports/fii-dii", timeout=15)
    time.sleep(1)
    r = sess.get(NSE_FII_DII, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_fii_dii(rows: list[dict]) -> list[dict]:
    parsed = []
    for row in rows:
        date_raw = row.get("date", "")
        try:
            date_iso = dt.datetime.strptime(date_raw, "%d-%b-%Y").date().isoformat()
        except ValueError:
            date_iso = date_raw
        parsed.append(
            {
                "date": date_iso,
                "category": row.get("category", "").strip(),
                "buy_value": float(row.get("buyValue", 0) or 0),
                "sell_value": float(row.get("sellValue", 0) or 0),
                "net_value": float(row.get("netValue", 0) or 0),
            }
        )
    return parsed


def append_history(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    existing: set[tuple[str, str]] = set()
    if HISTORY_CSV.exists():
        with HISTORY_CSV.open() as f:
            for r in csv.DictReader(f):
                existing.add((r["date"], r["category"]))

    new_rows = [r for r in rows if (r["date"], r["category"]) not in existing]
    write_header = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["date", "category", "buy_value", "sell_value", "net_value"]
        )
        if write_header:
            w.writeheader()
        for r in new_rows:
            w.writerow(r)
    print(f"FII/DII: appended {len(new_rows)} new rows")


def write_latest_fii_dii(rows: list[dict]) -> dict:
    latest_date = max(r["date"] for r in rows)
    latest = [r for r in rows if r["date"] == latest_date]
    LATEST_JSON.write_text(json.dumps({"date": latest_date, "rows": latest}, indent=2))
    return {"date": latest_date, "rows": latest}


def build_snapshot(fii_dii_latest: dict) -> dict:
    print("Fetching indices...")
    indices = fetch_indices()
    print(f"  {len(indices)} indices")

    print("Fetching breadth...")
    breadth = fetch_breadth()

    print("Fetching top movers...")
    movers = fetch_movers()

    print("Fetching global cues...")
    global_idx = fetch_global()

    print("Fetching FX + commodities...")
    fx_comm = fetch_fx_comm()

    vix = indices.get("INDIA VIX", {})
    mood = market_mood(indices, vix.get("pct"))

    snapshot = {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "date": fii_dii_latest.get("date"),
        "mood": mood,
        "indices": indices,
        "vix": vix,
        "breadth": breadth,
        "movers": movers,
        "global": global_idx,
        "fx_commodities": fx_comm,
        "fii_dii": fii_dii_latest,
    }
    SNAPSHOT_JSON.write_text(json.dumps(snapshot, indent=2, default=str))

    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    if snapshot["date"]:
        (SNAPSHOTS_DIR / f"{snapshot['date']}.json").write_text(
            json.dumps(snapshot, indent=2, default=str)
        )
    print(f"Wrote snapshot for {snapshot['date']} — mood: {mood}")
    return snapshot


def main() -> int:
    try:
        raw = fetch_fii_dii()
    except Exception as e:
        print(f"FII/DII fetch failed: {e}", file=sys.stderr)
        return 1
    rows = parse_fii_dii(raw)
    if not rows:
        print("No FII/DII rows parsed", file=sys.stderr)
        return 1
    append_history(rows)
    fii_dii_latest = write_latest_fii_dii(rows)

    build_snapshot(fii_dii_latest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
