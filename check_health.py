#!/usr/bin/env python3
"""
check_health.py — tracks consecutive scraper failures across scheduled runs.

Called by the GitHub Actions workflow right after each scraper run, with the
outcome ("success" or "failure") passed as a command-line argument:

    python check_health.py success
    python check_health.py failure

It keeps a small running count in data/health_status.json:
  - On success: resets the counter to 0.
  - On failure: increments the counter.

It prints `consecutive_failures=<n>` and also writes that to $GITHUB_OUTPUT
so the workflow can decide whether to raise an alert (e.g. after 3 in a row).
"""

import sys
import json
import os
from datetime import datetime, timezone

PATH = "data/health_status.json"


def main():
    outcome = sys.argv[1] if len(sys.argv) > 1 else "failure"

    try:
        with open(PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if outcome == "success":
        state["consecutive_failures"] = 0
        state["last_success"] = now
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_failure"] = now

    os.makedirs("data", exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    n = state["consecutive_failures"]
    print(f"consecutive_failures={n}")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"consecutive_failures={n}\n")


if __name__ == "__main__":
    main()
