"""Fetch FII/DII cash-market data from NSE and append to history CSV."""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

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
HISTORY_CSV = DATA_DIR / "fii_dii_history.csv"
LATEST_JSON = DATA_DIR / "latest.json"


def fetch() -> list[dict]:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.get(NSE_HOME, timeout=15)
    time.sleep(1)
    sess.get("https://www.nseindia.com/reports/fii-dii", timeout=15)
    time.sleep(1)
    r = sess.get(NSE_FII_DII, timeout=15)
    r.raise_for_status()
    return r.json()


def parse(rows: list[dict]) -> list[dict]:
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

    print(f"Appended {len(new_rows)} new rows to {HISTORY_CSV}")


def write_latest(rows: list[dict]) -> None:
    latest_date = max(r["date"] for r in rows)
    latest = [r for r in rows if r["date"] == latest_date]
    LATEST_JSON.write_text(json.dumps({"date": latest_date, "rows": latest}, indent=2))
    print(f"Wrote latest snapshot for {latest_date}")


def main() -> int:
    try:
        raw = fetch()
    except Exception as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 1
    rows = parse(raw)
    if not rows:
        print("No rows parsed", file=sys.stderr)
        return 1
    append_history(rows)
    write_latest(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
