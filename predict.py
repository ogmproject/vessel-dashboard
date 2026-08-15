#!/usr/bin/env python3
"""
predict.py — Vessel arrival prediction based on historical berthing/departure patterns.

MATCHED AGAINST THE REAL data/vessel_data.json SCHEMA
--------------------------------------------------------
    {
      "generated_at": "...",
      "tps": {
        "alongside": [{"name": "I3 - MSC SPARKLE III", "atb": "11/08/2026 02:35", "etd": "...", ...}],
        "schedule":  [{"name": "...", "eta": "...", "etd": "...", ...}],
        "movement":  [{"name": "MSC CHERYL 3", "berth": "10/08/2026 14:05", "dep": "11/08/2026 04:25"}]
      },
      "teluk_lamong": {
        "alongside": [{"name": "KMTC SHIMIZU", "atb": "...", "etd": "...", "type": "INTERNATIONAL", ...}],
        "schedule":  [...],
        "departed":  [{"name": "...", ...}]   # same idea as tps.movement, may use different field names
      }
    }

KEY INSIGHT: unlike a generic "diff two snapshots" approach, TPS's own
"movement" list (and presumably Teluk Lamong's "departed" list) ALREADY gives
completed, source-verified arrival+departure pairs. We use those directly —
no snapshot-diffing needed, no risk of missing an event between 15-minute
scrapes.

Two catches handled below:
  1. TPS "alongside" vessel names carry a berth-number prefix, e.g.
     "I3 - MSC SPARKLE III" — stripped to "MSC SPARKLE III" so it matches
     the (unprefixed) name used in "movement".
  2. "movement" / "departed" arrays are a ROLLING WINDOW (only the most
     recent N entries) — older entries fall off over time. So every run,
     we merge any *new* entries into a permanently growing local log
     (data/vessel_history_log.json) before they scroll away.

HOW IT FITS INTO THE EXISTING PIPELINE
---------------------------------------
Runs right after scraper.py in the same GitHub Actions workflow. Reads the
snapshot scraper.py just wrote, updates the permanent history log, and
writes data/predictions.json for the dashboard to render.

USAGE
-----
    python predict.py
"""

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone

DATA_DIR = "data"
CURRENT_SNAPSHOT_PATH = os.path.join(DATA_DIR, "vessel_data.json")
HISTORY_LOG_PATH = os.path.join(DATA_DIR, "vessel_history_log.json")
PREDICTIONS_OUTPUT_PATH = os.path.join(DATA_DIR, "predictions.json")

# Minimum number of past arrivals needed before we trust a prediction at all.
MIN_VISITS_FOR_PREDICTION = 3

# If the spread (std dev) of intervals is wider than this many days, the
# vessel's schedule is too irregular to call it "high confidence".
HIGH_CONFIDENCE_STDDEV_THRESHOLD_DAYS = 2.0

# Ignore vessels not seen in this many days when predicting — likely no
# longer calling at this port.
STALE_VESSEL_CUTOFF_DAYS = 120

# Matches TPS's berth-number prefix, e.g. "I3 - ", "I12 - "
TPS_BERTH_PREFIX_RE = re.compile(r"^I?\d*\s*-\s*")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def clean_vessel_name(raw_name):
    """Strips TPS's berth-number prefix, e.g. 'I3 - MSC SPARKLE III' -> 'MSC SPARKLE III'.
    No-op for names that don't have the prefix (e.g. Teluk Lamong, or TPS movement entries)."""
    if not raw_name:
        return raw_name
    return TPS_BERTH_PREFIX_RE.sub("", raw_name).strip()


def parse_dt(value):
    """Best-effort parse of the timestamp formats seen in vessel_data.json
    (e.g. '11/08/2026 02:35') or ISO 8601. Returns None on failure."""
    if not value:
        return None
    formats = ("%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
    for fmt in formats:
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def visit_fingerprint(terminal, vessel_name, arrival_dt):
    """Unique key for deduplication: same vessel + same terminal + same
    arrival timestamp = same visit, no matter how many times we see it
    across runs."""
    arrival_key = arrival_dt.isoformat() if arrival_dt else "unknown"
    return f"{terminal}:{vessel_name}:{arrival_key}"


def collect_completed_visits(snapshot):
    """
    Pulls fully-resolved (arrival + departure both known) visits from
    tps.movement and teluk_lamong.departed.
    """
    visits = []

    for entry in snapshot.get("tps", {}).get("movement", []):
        name = clean_vessel_name(entry.get("name"))
        arrival = parse_dt(entry.get("berth"))
        departure = parse_dt(entry.get("dep"))
        if name and arrival:
            visits.append({
                "vessel_name": name,
                "terminal": "TPS",
                "arrival": arrival,
                "departure": departure,
            })

    # Teluk Lamong's "departed" list is empty in the sample we've seen so far,
    # so its exact field names are unconfirmed. We defensively check a few
    # likely conventions (matching the "alongside" list's atb/etd, or the
    # same berth/dep convention TPS uses). VERIFY once this list is non-empty
    # and adjust if the real field names differ.
    for entry in snapshot.get("teluk_lamong", {}).get("departed", []):
        name = clean_vessel_name(entry.get("name"))
        arrival = parse_dt(entry.get("atb") or entry.get("berth") or entry.get("arrival"))
        departure = parse_dt(entry.get("atd") or entry.get("dep") or entry.get("departure") or entry.get("etd"))
        if name and arrival:
            visits.append({
                "vessel_name": name,
                "terminal": "TL",
                "arrival": arrival,
                "departure": departure,
            })

    return visits


def collect_in_progress_visits(snapshot):
    """
    Pulls vessels currently "alongside" (arrived, not yet departed) as
    provisional data points. These still tell us a vessel arrived on a given
    date, which is useful for the interval pattern even before departure is
    confirmed. When the vessel later appears in movement/departed with the
    same arrival timestamp, the fingerprint-based dedup in main() means it
    won't be double-counted — the completed-visit version (which also has
    departure info) simply overwrites the provisional one.
    """
    visits = []

    for entry in snapshot.get("tps", {}).get("alongside", []):
        name = clean_vessel_name(entry.get("name"))
        arrival = parse_dt(entry.get("atb"))
        if name and arrival:
            visits.append({
                "vessel_name": name,
                "terminal": "TPS",
                "arrival": arrival,
                "departure": None,
            })

    for entry in snapshot.get("teluk_lamong", {}).get("alongside", []):
        name = clean_vessel_name(entry.get("name"))
        arrival = parse_dt(entry.get("atb"))
        if name and arrival:
            visits.append({
                "vessel_name": name,
                "terminal": "TL",
                "arrival": arrival,
                "departure": None,
            })

    return visits


def merge_into_history_log(history_log, new_visits, now_iso):
    """
    Merges newly-seen visits into the permanent log, keyed by fingerprint.
    A completed visit (with departure) always overwrites a provisional one
    (without departure) for the same fingerprint. Returns (updated_log, added_count).
    """
    by_fingerprint = {
        visit_fingerprint(v["terminal"], v["vessel_name"], parse_dt(v["arrival"])): v
        for v in history_log
    }

    added = 0
    for v in new_visits:
        fp = visit_fingerprint(v["terminal"], v["vessel_name"], v["arrival"])
        existing = by_fingerprint.get(fp)
        is_new = existing is None
        is_upgrade = existing is not None and existing.get("departure") is None and v["departure"] is not None

        if is_new or is_upgrade:
            by_fingerprint[fp] = {
                "vessel_name": v["vessel_name"],
                "terminal": v["terminal"],
                "arrival": v["arrival"].isoformat(),
                "departure": v["departure"].isoformat() if v["departure"] else None,
                "recorded_at": now_iso,
            }
            added += 1

    updated_log = list(by_fingerprint.values())
    updated_log.sort(key=lambda v: v["arrival"])
    return updated_log, added


def compute_predictions(history_log, now):
    """
    Groups visits by vessel+terminal, computes the mean interval between
    consecutive arrivals, and projects the next expected arrival date.
    Also reports average dwelling time (berth to departure) as a bonus
    stat when departure data is available.
    """
    by_vessel = {}
    for v in history_log:
        arrival = parse_dt(v.get("arrival"))
        if arrival is None:
            continue
        key = f"{v['terminal']}:{v['vessel_name']}"
        by_vessel.setdefault(key, []).append(v)

    predictions = []
    for key, visits in by_vessel.items():
        visits.sort(key=lambda v: v["arrival"])
        arrivals = [parse_dt(v["arrival"]) for v in visits]
        last_seen = arrivals[-1]

        if (now - last_seen).days > STALE_VESSEL_CUTOFF_DAYS:
            continue
        if len(arrivals) < MIN_VISITS_FOR_PREDICTION:
            continue

        intervals_days = [
            (arrivals[i] - arrivals[i - 1]).total_seconds() / 86400
            for i in range(1, len(arrivals))
            if (arrivals[i] - arrivals[i - 1]).total_seconds() > 0
        ]
        if not intervals_days:
            continue

        avg_interval = statistics.mean(intervals_days)
        stdev_interval = statistics.pstdev(intervals_days) if len(intervals_days) > 1 else 0.0
        predicted_next = last_seen + timedelta(days=avg_interval)

        confidence = (
            "high" if len(arrivals) >= 4 and stdev_interval <= HIGH_CONFIDENCE_STDDEV_THRESHOLD_DAYS
            else "medium" if len(arrivals) >= MIN_VISITS_FOR_PREDICTION
            else "low"
        )

        # Bonus: average dwelling time (hours) across visits where we know
        # both arrival and departure.
        dwelling_hours = []
        for v in visits:
            arr = parse_dt(v["arrival"])
            dep = parse_dt(v.get("departure"))
            if arr and dep and dep > arr:
                dwelling_hours.append((dep - arr).total_seconds() / 3600)
        avg_dwelling_hours = round(statistics.mean(dwelling_hours), 1) if dwelling_hours else None

        terminal, vessel_name = key.split(":", 1)
        predictions.append({
            "vessel_name": vessel_name,
            "terminal": terminal,
            "last_arrival": last_seen.isoformat(),
            "predicted_next_arrival": predicted_next.isoformat(),
            "avg_interval_days": round(avg_interval, 1),
            "interval_stddev_days": round(stdev_interval, 1),
            "avg_dwelling_hours": avg_dwelling_hours,
            "visits_recorded": len(arrivals),
            "confidence": confidence,
        })

    predictions.sort(key=lambda p: p["predicted_next_arrival"])
    return predictions


def main():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat()

    snapshot = load_json(CURRENT_SNAPSHOT_PATH, default={})
    history_log = load_json(HISTORY_LOG_PATH, default=[])

    new_visits = collect_completed_visits(snapshot) + collect_in_progress_visits(snapshot)
    history_log, added = merge_into_history_log(history_log, new_visits, now_iso)
    save_json(HISTORY_LOG_PATH, history_log)
    print(f"History log: {len(history_log)} total visit(s) tracked "
          f"({added} new/updated this run).")

    predictions = compute_predictions(history_log, now)
    save_json(PREDICTIONS_OUTPUT_PATH, {
        "generated_at": now_iso,
        "predictions": predictions,
    })
    print(f"Wrote {len(predictions)} prediction(s) to {PREDICTIONS_OUTPUT_PATH}.")


if __name__ == "__main__":
    main()
