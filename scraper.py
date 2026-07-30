#!/usr/bin/env python3
"""
scraper.py — pulls vessel schedule data from TPS Surabaya & Teluk Lamong's
public (no-login) webaccess pages, filters international vessels for
Teluk Lamong, and writes a single JSON file that the dashboard reads.

This is meant to be run automatically by the GitHub Actions workflow in
.github/workflows/update-data.yml (every 15 minutes), NOT manually every time.

Output: data/vessel_data.json
"""

import re
import json
import sys
from datetime import datetime, timezone

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependencies. Run: pip install -r requirements.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

TPS_URL = "https://webaccess.tps.co.id/webaccess/"
TL_URL = "https://app.teluklamong.co.id/webaccess/"

OUTPUT_PATH = "data/vessel_data.json"


def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.text


def clean_lines(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    return [l for l in text.split("\n") if l.strip()]


# ---------------------------------------------------------------------------
# TPS SURABAYA
# NOTE: TPS's public page does NOT label routes Domestic/International.
# Every vessel gets verified:false so the dashboard can flag it clearly.
# ---------------------------------------------------------------------------
def parse_tps(html: str):
    lines = clean_lines(html)
    alongside, schedule = [], []
    current = {}
    section = None

    for line in lines:
        if "Vessel Alongside" in line:
            section = "alongside"
            continue
        if "Vessel Schedule" in line:
            section = "schedule"
            continue

        m = re.match(r"^([A-Z0-9 .\-–]+?)\s+([A-Z0-9\-]+\s*/\s*[A-Z0-9\-]+)$", line)
        if m and "ETA" not in line and "ETD" not in line and "ATB" not in line:
            if current.get("name"):
                (alongside if section == "alongside" else schedule).append(current)
            current = {"name": m.group(1).strip(), "code": m.group(2).strip(), "verified": False}
        elif "ATB :" in line:
            current["atb"] = line.split(":", 1)[1].strip()
        elif "ETA :" in line:
            current["eta"] = line.split(":", 1)[1].strip()
        elif "ETD :" in line:
            current["etd"] = line.split(":", 1)[1].strip()
        elif "Open Stack :" in line:
            current["openstack"] = line.split(":", 1)[1].strip()
        elif "Closing Time Container" in line:
            current["closeC"] = line.split(":", 1)[1].strip()
        elif "Closing Time Document" in line:
            current["closeD"] = line.split(":", 1)[1].strip()

    if current.get("name"):
        (alongside if section == "alongside" else schedule).append(current)

    return alongside, schedule


# ---------------------------------------------------------------------------
# TELUK LAMONG
# This page DOES label each vessel DOMESTIC / INTERNATIONAL — we keep only
# INTERNATIONAL entries.
# ---------------------------------------------------------------------------
def parse_teluk_lamong(html: str):
    lines = clean_lines(html)
    alongside, schedule, departed = [], [], []
    current = {}
    section = None
    name_code_re = re.compile(r"^([A-Z0-9 .\-]+)\s*/\s*([A-Z0-9]+)$")

    def flush():
        """Push the current vessel into its list if it's a complete INTERNATIONAL entry."""
        if current.get("name") and current.get("type") == "INTERNATIONAL":
            {"alongside": alongside, "departed": departed, "schedule": schedule}[section or "schedule"].append(current)

    i = 0
    while i < len(lines):
        line = lines[i]

        if "Vessel Alongside" in line:
            section = "alongside"
            i += 1
            continue
        if "Vessel Has Been Departed" in line:
            section = "departed"
            i += 1
            continue
        if "Vessel Schedule" in line:
            section = "schedule"
            i += 1
            continue

        m = name_code_re.match(line)
        if m and len(line) < 60:
            # Only treat this as the START of a NEW vessel if the previous one is
            # already fully parsed (has a type) or there is no vessel in progress.
            # Otherwise this is the voyage-number line (e.g. "46S / 46N") that
            # follows the real "NAME / CODE" header — keep the real name, don't overwrite it.
            if not current or current.get("type"):
                flush()
                current = {"name": m.group(1).strip(), "code": m.group(2).strip()}
            else:
                current.setdefault("voyage", line.strip())
            i += 1
            continue

        if line in ("DOMESTIC", "INTERNATIONAL"):
            current["type"] = line
            i += 1
            continue

        for label, key in (("ATB :", "atb"), ("ETB :", "etb"), ("ETD :", "etd"), ("ATD :", "etd"),
                           ("Open Stack :", "openstack"), ("Closing Time Container", "closeC")):
            if line.startswith(label) or (label == "Closing Time Container" and label in line):
                value = line.split(":", 1)[1].strip() if ":" in line else ""
                if not value and i + 1 < len(lines):
                    # The value sits on its own line right after the label —
                    # this happens with some fields on Teluk Lamong's page.
                    value = lines[i + 1].strip()
                    i += 1
                current[key] = value
                break
        else:
            if "PT" in line or "LTD" in line.upper() or "CO." in line.upper():
                current.setdefault("carrier", line.strip())
        i += 1

    flush()
    return alongside, schedule, departed


def main():
    print("Fetching TPS Surabaya ...")
    tps_html = fetch(TPS_URL)
    tps_alongside, tps_schedule = parse_tps(tps_html)

    print("Fetching Teluk Lamong ...")
    tl_html = fetch(TL_URL)
    tl_alongside, tl_schedule, tl_departed = parse_teluk_lamong(tl_html)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tps": {
            "alongside": tps_alongside,
            "schedule": tps_schedule,
            "note": "TPS's public page does not label Domestic/International — verify manually.",
        },
        "teluk_lamong": {
            "alongside": tl_alongside,
            "schedule": tl_schedule,
            "departed": tl_departed,
            "note": "Filtered to INTERNATIONAL-labeled vessels only.",
        },
    }

    import os
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote {OUTPUT_PATH}: "
          f"{len(tps_alongside)} TPS alongside, {len(tps_schedule)} TPS schedule, "
          f"{len(tl_alongside)} TL alongside (intl), {len(tl_schedule)} TL schedule (intl), "
          f"{len(tl_departed)} TL departed (intl)")


if __name__ == "__main__":
    main()
