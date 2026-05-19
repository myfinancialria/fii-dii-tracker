"""Attach a *why* to top movers and top/worst sectors.

Pipeline:
  1. Pull last-24h headlines from Google News RSS for each ticker / sector.
  2. If ANTHROPIC_API_KEY is set → ask Claude Haiku to condense the headlines
     into a one-line catalyst (max ~12 words).
  3. Otherwise → fall back to the cleanest headline as-is.

Degrades gracefully: if news fetch or LLM call fails, leaves catalyst as None
and the rest of the dashboard still works.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import requests

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT_JSON = DATA_DIR / "snapshot.json"

GOOGLE_NEWS = "https://news.google.com/rss/search"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

SECTOR_LABELS = {
    "NIFTY IT": "Indian IT stocks",
    "NIFTY BANK": "Indian banks Nifty Bank",
    "NIFTY AUTO": "Indian auto stocks",
    "NIFTY PHARMA": "Indian pharma stocks",
    "NIFTY FMCG": "Indian FMCG stocks",
    "NIFTY METAL": "Indian metal stocks",
    "NIFTY REALTY": "Indian realty stocks",
    "NIFTY ENERGY": "Indian energy stocks",
    "NIFTY PSU BANK": "Indian PSU banks",
    "NIFTY FINANCIAL SERVICES": "Indian financial services",
}


def fetch_headlines(query: str, max_items: int = 5) -> list[dict]:
    q = urllib.parse.quote_plus(f"{query} when:1d")
    url = f"{GOOGLE_NEWS}?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        out = []
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if " - " in title:
                title_clean, source = title.rsplit(" - ", 1)
            else:
                title_clean, source = title, ""
            out.append({"title": title_clean.strip(), "link": link,
                        "source": source.strip(), "published": pub})
        return out
    except Exception as e:
        print(f"  headlines failed for '{query}': {e}")
        return []


def summarize_with_claude(name: str, kind: str, pct: float,
                          headlines: list[dict]) -> Optional[str]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if not headlines:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        print("  anthropic SDK not installed — skipping LLM summary")
        return None
    titles = "\n".join(f"- {h['title']}" for h in headlines)
    direction = "up" if pct >= 0 else "down"
    prompt = (
        f"Recent news headlines about {name} ({kind}), which moved "
        f"{direction} {abs(pct):.1f}% today on NSE:\n\n"
        f"{titles}\n\n"
        "In ONE short sentence (max 12 words), describe the most likely catalyst "
        "for today's move. If no headline clearly explains today's move, "
        "reply exactly: \"No clear catalyst\". Reply with just the sentence — "
        "no prefix, no markdown."
    )
    try:
        client = Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip().rstrip(".")
        return text if text and text.lower() != "no clear catalyst" else None
    except Exception as e:
        print(f"  Claude call failed for {name}: {e}")
        return None


def attach(name: str, kind: str, pct: float, query: str) -> dict:
    headlines = fetch_headlines(query)
    catalyst = summarize_with_claude(name, kind, pct, headlines)
    top = headlines[0] if headlines else {}
    fallback_headline = top.get("title")
    return {
        "catalyst": catalyst,
        "headline": fallback_headline,
        "source": top.get("source"),
        "link": top.get("link"),
    }


def enrich(snap: dict) -> dict:
    movers = snap.get("movers", {})
    print(f"Catalysts for {len(movers.get('gainers', []))} gainers "
          f"+ {len(movers.get('losers', []))} losers...")
    for row in movers.get("gainers", [])[:3] + movers.get("losers", [])[:3]:
        sym = row.get("symbol", "")
        info = attach(sym, "stock", row.get("pct", 0), f"{sym} stock NSE India")
        row.update(info)
        time.sleep(0.4)

    indices = snap.get("indices", {})
    sectors = [(n, d.get("pct", 0)) for n, d in indices.items()
               if n in SECTOR_LABELS]
    sectors.sort(key=lambda x: x[1])
    bottom = sectors[:2]
    top = sectors[-2:][::-1]
    flagged = {n for n, _ in top + bottom}
    print(f"Catalysts for sectors: top={[n for n,_ in top]} "
          f"bottom={[n for n,_ in bottom]}")
    for name, pct in top + bottom:
        info = attach(SECTOR_LABELS[name], "sector", pct, SECTOR_LABELS[name])
        indices[name].update(info)
        time.sleep(0.4)

    snap["catalyst_sectors"] = {
        "top": [{"name": n, "pct": indices[n].get("pct"),
                 "catalyst": indices[n].get("catalyst"),
                 "headline": indices[n].get("headline"),
                 "source": indices[n].get("source"),
                 "link": indices[n].get("link")}
                for n, _ in top if n in flagged],
        "bottom": [{"name": n, "pct": indices[n].get("pct"),
                    "catalyst": indices[n].get("catalyst"),
                    "headline": indices[n].get("headline"),
                    "source": indices[n].get("source"),
                    "link": indices[n].get("link")}
                   for n, _ in bottom if n in flagged],
    }
    return snap


def main() -> int:
    if not SNAPSHOT_JSON.exists():
        print("snapshot.json missing — run scraper.py first", file=sys.stderr)
        return 1
    snap = json.loads(SNAPSHOT_JSON.read_text())
    snap = enrich(snap)
    SNAPSHOT_JSON.write_text(json.dumps(snap, indent=2, default=str))

    # Mirror to dated archive if present
    snaps_dir = DATA_DIR / "snapshots"
    if snaps_dir.exists() and snap.get("date"):
        (snaps_dir / f"{snap['date']}.json").write_text(
            json.dumps(snap, indent=2, default=str)
        )
    print("Catalysts attached.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
