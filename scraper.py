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
import time
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
    """
    TPS's page text doesn't reliably break onto separate lines per field the
    way Teluk Lamong's does — sometimes a whole vessel record (name, code,
    dates) comes through as one long run of text. So instead of reading
    line-by-line, we normalize everything into one whitespace-collapsed
    string and pull out each vessel record with a single regex, anchored on
    the labels ("ETA :", "ETD :", etc.) that are always present.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)

    def slice_section(full_text: str, start_marker: str, end_markers: list) -> str:
        start = full_text.find(start_marker)
        if start == -1:
            return ""
        start += len(start_marker)
        end = len(full_text)
        for marker in end_markers:
            idx = full_text.find(marker, start)
            if idx != -1:
                end = min(end, idx)
        return full_text[start:end]

    date_re = r"\d{2}/\d{2}/\d{4}\s*\d{2}:\d{2}"

    alongside_section = slice_section(text, "Vessel Alongside", ["Vessel Schedule", "Container / Cargo"])
    schedule_section = slice_section(text, "Vessel Schedule", ["Container / Cargo", "Quarantine"])

    alongside_pattern = re.compile(
        r"([A-Z][A-Z0-9 .\-]{2,40}?)\s+([A-Z0-9\-]+\s*/\s*[A-Z0-9\-]+)\s+"
        r"ATB\s*:\s*(" + date_re + r")\s+ETD\s*:\s*(" + date_re + r")"
    )
    schedule_pattern = re.compile(
        r"([A-Z][A-Z0-9 .\-]{2,40}?)\s+([A-Z0-9\-]+\s*/\s*[A-Z0-9\-]+)\s+"
        r"ETA\s*:\s*(" + date_re + r")\s+ETD\s*:\s*(" + date_re + r")\s+"
        r"Open Stack\s*:\s*(" + date_re + r")\s+"
        r"Closing Time Container\s*:\s*(" + date_re + r")\s+"
        r"Closing Time Document\s*:\s*(" + date_re + r")"
    )

    alongside = []
    for m in alongside_pattern.finditer(alongside_section):
        alongside.append({
            "name": m.group(1).strip(" -–"),
            "code": m.group(2).strip(),
            "atb": m.group(3).strip(),
            "etd": m.group(4).strip(),
            "verified": False,
        })

    schedule = []
    for m in schedule_pattern.finditer(schedule_section):
        schedule.append({
            "name": m.group(1).strip(" -–"),
            "code": m.group(2).strip(),
            "eta": m.group(3).strip(),
            "etd": m.group(4).strip(),
            "openstack": m.group(5).strip(),
            "closeC": m.group(6).strip(),
            "closeD": m.group(7).strip(),
            "verified": False,
        })

    # Diagnostics: if either section came back empty, print a snippet so the
    # GitHub Actions log shows us what the page actually looked like this time.
    if not alongside:
        print("[warn] TPS alongside: 0 vessels parsed. Section snippet:")
        print(alongside_section[:500])
    if not schedule:
        print("[warn] TPS schedule: 0 vessels parsed. Section snippet:")
        print(schedule_section[:500])

    return alongside, schedule, text


def parse_tps_movement(full_text: str):
    """
    TPS's site has a scrolling ticker of recently berthed/departed vessels,
    formatted like:
        SKY WIND ~BERTH : 25/07/2026 23:03 ~DEPARTURE : 26/07/2026 15:00
    The site itself only keeps a handful of the most recent entries there
    (effectively the last ~24 hours), so we just parse whatever's present —
    no extra date filtering needed on our side.
    """
    date_re = r"\d{2}/\d{2}/\d{4}\s*\d{2}:\d{2}"
    pattern = re.compile(
        r"([A-Z][A-Z0-9 .\-]{2,40}?)\s*~\s*BERTH\s*:\s*(" + date_re + r")\s*~\s*DEPARTURE\s*:\s*(" + date_re + r")"
    )
    movement = []
    for m in pattern.finditer(full_text):
        movement.append({
            "name": m.group(1).strip(" -–"),
            "berth": m.group(2).strip(),
            "dep": m.group(3).strip(),
        })

    if not movement:
        print("[warn] TPS movement ticker: 0 entries parsed.")

    return movement


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


MMSI_CACHE_PATH = "data/mmsi_cache.json"
MAX_LOOKUPS_PER_RUN = 6   # keep this low & polite — spreads work across many runs
REQUEST_DELAY_SECONDS = 1.5


def load_mmsi_cache() -> dict:
    try:
        with open(MMSI_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_mmsi_cache(cache: dict):
    import os
    os.makedirs("data", exist_ok=True)
    with open(MMSI_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def lookup_mmsi(name: str) -> dict:
    """
    Best-effort lookup of a vessel's MMSI/IMO via VesselFinder's public search
    page. This is screen-scraping a search results page (not an official API),
    so it's wrapped defensively: any failure just returns an empty result and
    the dashboard falls back to its "Search" links — nothing breaks.
    """
    try:
        url = f"https://www.vesselfinder.com/vessels?name={name.replace(' ', '+')}"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return {}
        html = resp.text
        # VesselFinder ship-detail links typically encode both IDs in the URL,
        # e.g. .../vessels/details/0-IMO-9871490-MMSI-563124300 (exact slug
        # format may vary, so we search loosely for the two ID patterns close
        # together rather than depending on one exact URL shape).
        m = re.search(r"IMO-(\d{7}).{0,40}?MMSI-(\d{5,9})", html, re.IGNORECASE)
        if not m:
            m = re.search(r"MMSI-(\d{5,9}).{0,40}?IMO-(\d{7})", html, re.IGNORECASE)
            if m:
                return {"mmsi": m.group(1), "imo": m.group(2)}
            return {}
        return {"imo": m.group(1), "mmsi": m.group(2)}
    except Exception as e:
        print(f"[warn] MMSI lookup failed for '{name}': {e}")
        return {}


def enrich_with_mmsi(*vessel_lists) -> int:
    """
    For every vessel across the given lists, attach mmsi/imo from the cache
    if known. For names not yet in the cache, look them up (up to
    MAX_LOOKUPS_PER_RUN per run, to stay polite and keep runs fast) and save
    any new results back to the cache file for next time.
    """
    cache = load_mmsi_cache()
    lookups_done = 0
    new_results = 0

    all_names = []
    for vessels in vessel_lists:
        for v in vessels:
            if v.get("name") and v["name"] not in all_names:
                all_names.append(v["name"])

    for name in all_names:
        if name not in cache:
            if lookups_done >= MAX_LOOKUPS_PER_RUN:
                continue
            result = lookup_mmsi(name)
            cache[name] = result  # cache even empty results, so we don't retry forever
            lookups_done += 1
            if result:
                new_results += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    for vessels in vessel_lists:
        for v in vessels:
            info = cache.get(v.get("name"), {})
            if info.get("mmsi"):
                v["mmsi"] = info["mmsi"]
            if info.get("imo"):
                v["imo"] = info["imo"]

    save_mmsi_cache(cache)
    print(f"MMSI lookups this run: {lookups_done} attempted, {new_results} found. "
          f"Cache now has {len(cache)} vessel names.")
    return new_results


def main():
    print("Fetching TPS Surabaya ...")
    tps_html = fetch(TPS_URL)
    tps_alongside, tps_schedule, tps_full_text = parse_tps(tps_html)
    tps_movement = parse_tps_movement(tps_full_text)

    print("Fetching Teluk Lamong ...")
    tl_html = fetch(TL_URL)
    tl_alongside, tl_schedule, tl_departed = parse_teluk_lamong(tl_html)

    print("Looking up MMSI/IMO for vessels not yet in cache ...")
    enrich_with_mmsi(tps_alongside, tps_schedule, tps_movement, tl_alongside, tl_schedule, tl_departed)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tps": {
            "alongside": tps_alongside,
            "schedule": tps_schedule,
            "movement": tps_movement,
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
