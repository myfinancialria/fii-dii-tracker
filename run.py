"""Orchestrator: scrape → visualize → post to Slack."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def step(name: str, script: str) -> None:
    print(f"\n=== {name} ===")
    r = subprocess.run([sys.executable, str(HERE / script)], cwd=str(HERE))
    if r.returncode != 0:
        sys.exit(r.returncode)


def main() -> None:
    step("Scrape NSE", "scraper.py")
    step("Build chart + dashboard", "visualize.py")
    try:
        step("Post to Slack", "slack_post.py")
    except SystemExit as e:
        if e.code != 0:
            print("Slack step failed (continuing)", file=sys.stderr)


if __name__ == "__main__":
    main()
