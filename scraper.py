#!/usr/bin/env python3
"""
scraper.py — pulls vessel schedule data from TPS Surabaya & Teluk Lamong's
public (no-login) webaccess pages, filters international vessels for
Teluk Lamong, and writes a single JSON file that the dashboard reads.

This is meant to be run automatically by the GitHub Actions workflow in
.github/workflows/update-data.yml (every 30 minutes), NOT manually every time.

Output: data/vessel_data.json
"""

import re
import json
import sys
import time
from datetime import datetime, timezone, timedelta

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
    """
    FIXED (see notes below) — two bugs corrected from the original version:

    1. SECTION-BOUNDARY FLUSH BUG: previously, flush() was only called when
       a NEW vessel's "NAME / CODE" line was found — never when a section
       header changed. This meant the LAST vessel of any section (Alongside
       / Departed / Schedule) only got flushed AFTER the section variable
       had already moved on to the NEXT section, so it silently got filed
       under the wrong section. Symptom observed in production: a vessel
       that had genuinely departed (confirmed on the live site under
       "Vessel Has Been Departed") still showing up misclassified elsewhere
       in our dashboard.
       FIX: flush() (and reset the in-progress `current` dict) is now
       called at EVERY section-header transition, not just when a new
       vessel starts. This guarantees the last vessel of a section is
       filed under that section, before the section pointer moves on.

    2. ETD/ATD KEY COLLISION: "ETD :" (scheduled departure) and "ATD :"
       (actual departure) were both being written into the same "etd" key,
       so a vessel's real departure timestamp could get silently overwritten
       by/confused with its scheduled ETD.
       FIX: "ATD :" now writes to its own "atd" key, kept separate from
       "etd".
    """
    lines = clean_lines(html)
    alongside, schedule, departed = [], [], []
    current = {}
    section = None
    name_code_re = re.compile(r"^([A-Z0-9 .\-]+)\s*/\s*([A-Z0-9]+)$")

    def flush():
        """Push the current vessel into its list if it's a complete INTERNATIONAL entry."""
        if current.get("name") and current.get("type") == "INTERNATIONAL":
            {"alongside": alongside, "departed": departed, "schedule": schedule}[section or "schedule"].append(current)

    def change_section(new_section):
        """Flush whatever vessel was in progress under the OLD section
        before switching — this is the fix for bug #1 above."""
        nonlocal current, section
        flush()
        current = {}
        section = new_section

    i = 0
    while i < len(lines):
        line = lines[i]

        if "Vessel Alongside" in line:
            change_section("alongside")
            i += 1
            continue
        if "Vessel Has Been Departed" in line:
            change_section("departed")
            i += 1
            continue
        if "Vessel Schedule" in line:
            change_section("schedule")
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

        for label, key in (("ATB :", "atb"), ("ETB :", "etb"), ("ETD :", "etd"), ("ATD :", "atd"),
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

    flush()  # last vessel of the final section (usually "schedule")
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


MAX_LOCATION_LOOKUPS_PER_RUN = 10   # separate, smaller budget — location changes every run, MMSI doesn't


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def lookup_location(name: str, mmsi: str, imo: str) -> str:
    """
    Best-effort fetch of a vessel's current sea-area (e.g. "Java Sea") from
    MyShipTracking's public per-vessel page. This is NOT an official API —
    same caveats as the MMSI lookup above: if the page format changes or the
    request fails, we just return "" and the dashboard shows no location
    text for that vessel. Nothing else breaks.
    """
    try:
        slug = slugify(name)
        url = f"https://www.myshiptracking.com/vessels/{slug}-mmsi-{mmsi}-imo-{imo}"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return ""
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)

        m = re.search(
            r"current position of .{0,80}? is (?:currently )?(?:in|at) "
            r"([A-Z][A-Za-z .\-]{2,40}?)(?:\s+with coordinates|,|\s+reported|\.|$)",
            text,
        )
        if m:
            return m.group(1).strip()
    except Exception as e:
        print(f"[warn] Location lookup failed for '{name}': {e}")
    return ""


def enrich_with_location(*vessel_lists):
    """
    Adds a "location" field (e.g. "Java Sea") to vessels that have a known
    MMSI, for a limited number of vessels per run to stay polite. Unlike
    MMSI/IMO, this is NOT cached persistently — a vessel's position changes
    constantly, so every run gets a fresh best-effort look.
    """
    lookups_done = 0
    found = 0
    for vessels in vessel_lists:
        for v in vessels:
            if lookups_done >= MAX_LOCATION_LOOKUPS_PER_RUN:
                return
            if not (v.get("mmsi") and v.get("imo") and v.get("name")):
                continue
            loc = lookup_location(v["name"], v["mmsi"], v["imo"])
            lookups_done += 1
            if loc:
                v["location"] = loc
                found += 1
            time.sleep(REQUEST_DELAY_SECONDS)
    print(f"Location lookups this run: {lookups_done} attempted, {found} found.")


# ---------------------------------------------------------------------------
# DATA QUALITY SANITY CHECK
# Added after a real incident: a Teluk Lamong parsing bug (since fixed —
# see the comments in parse_teluk_lamong above) silently left a departed
# vessel (CELSIUS EINDHOVEN) miscategorized as "alongside" with no error or
# warning anywhere. This check catches that entire CLASS of problem going
# forward, regardless of what specifically causes it next time (a new
# parsing bug, or the source site itself being slow to update its own
# status) — it doesn't fix anything automatically, it just makes the
# problem loud and visible in the GitHub Actions log instead of silent.
# ---------------------------------------------------------------------------
WIB = timezone(timedelta(hours=7))  # Indonesia Western Time, no DST


def parse_id_datetime(value):
    """Parses the 'DD/MM/YYYY HH:MM' format used throughout both source sites."""
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M")
    except (ValueError, AttributeError, TypeError):
        return None


def check_stale_alongside(vessels, terminal_label, now_wib) -> int:
    """
    Flags any vessel still marked 'alongside' whose ETD (or ATD, if that's
    the field present) has already passed. A vessel legitimately alongside
    should have an ETD in the future; one with a past ETD suggests either a
    parsing/categorization bug on our side, or the source site hasn't
    updated its own status yet — either way, worth a human's attention.
    """
    stale_count = 0
    for v in vessels:
        etd_str = v.get("etd") or v.get("atd")
        etd = parse_id_datetime(etd_str)
        if etd is None:
            continue
        if etd < now_wib:
            hours_late = (now_wib - etd).total_seconds() / 3600
            print(f"[warn] STALE ALONGSIDE: '{v.get('name')}' ({terminal_label}) — "
                  f"ETD/ATD was {etd_str}, already {hours_late:.1f}h in the past. "
                  f"Possible parsing issue or source site hasn't updated yet.")
            stale_count += 1
    return stale_count


def main():
    print("Fetching TPS Surabaya ...")
    tps_html = fetch(TPS_URL)
    tps_alongside, tps_schedule, tps_full_text = parse_tps(tps_html)
    tps_movement = parse_tps_movement(tps_full_text)

    print("Fetching Teluk Lamong ...")
    tl_html = fetch(TL_URL)
    tl_alongside, tl_schedule, tl_departed = parse_teluk_lamong(tl_html)

    print("Checking for stale 'alongside' entries (ETD/ATD already passed) ...")
    now_wib = datetime.now(WIB).replace(tzinfo=None)
    stale_tps = check_stale_alongside(tps_alongside, "TPS", now_wib)
    stale_tl = check_stale_alongside(tl_alongside, "Teluk Lamong", now_wib)
    total_stale = stale_tps + stale_tl
    if total_stale:
        print(f"[warn] TOTAL: {total_stale} vessel(s) flagged as stale-alongside this run "
              f"— check the warnings above. This does not fail the job, but is worth reviewing.")
    else:
        print("No stale alongside entries found — looks healthy.")

    print("Looking up MMSI/IMO for vessels not yet in cache ...")
    enrich_with_mmsi(tps_alongside, tps_schedule, tps_movement, tl_alongside, tl_schedule, tl_departed)

    print("Looking up current location for vessels with known MMSI ...")
    # Prioritize vessels still underway (alongside/movement) — schedule
    # entries not yet arrived benefit most from a location hint.
    enrich_with_location(tps_alongside, tl_alongside, tps_movement, tl_departed, tps_schedule, tl_schedule)

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
