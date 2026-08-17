#!/usr/bin/env python3

import argparse
import os
import re
import requests
import sys
from datetime import date, datetime
from urllib.parse import urljoin

PROCARE_URL = "https://api-school.procareconnect.com/api/web/parent/daily_activities/"
PROCARE_KID_ID = os.environ.get("PROCARE_KID_ID", "PROCARE_KID_ID_REDACTED")
PROCARE_TOKEN = os.environ.get("PROCARE_TOKEN", "PROCARE_TOKEN_REDACTED")

PROCARE_ID_RE = re.compile(r"\[Procare ID: ([0-9a-fA-F-]{36})\]")

BABYBUDDY_URL = os.environ.get("BABYBUDDY_URL", "http://babybuddy.example.com:8000/").rstrip("/")
BABYBUDDY_TOKEN = os.environ.get("BABYBUDDY_TOKEN", "BABYBUDDY_TOKEN_REDACTED")
BABYBUDDY_CHILD_ID = int(os.environ.get("BABYBUDDY_CHILD_ID", "1"))
BABYBUDDY_TAG = os.environ.get("BABYBUDDY_TAG", "Daycare")

procare = requests.Session()
procare.headers.update({
    "User-Agent": "Mozilla/5.0 Procare-BabyBuddy-Importer",
    "Accept": "application/json",
    "Authorization": f"Bearer {PROCARE_TOKEN}",
    "Origin": "https://schools.procareconnect.com",
    "Referer": "https://schools.procareconnect.com/",
})

babybuddy = requests.Session()
babybuddy.headers.update({
    "Authorization": f"Token {BABYBUDDY_TOKEN}",
    "Accept": "application/json",
})


# ---------------------------------------------------------------------------
# Procare
# ---------------------------------------------------------------------------

def fetch_procare(day):
    params = {
        "kid_id": PROCARE_KID_ID,
        "filters[daily_activity][date_to]": day.isoformat(),
        "page": 1,
    }

    activities = []

    while True:
        response = procare.get(
            PROCARE_URL,
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()

        activities.extend(payload.get("daily_activities", []))

        # The API currently returns per_page=30.
        # Stop when the page isn't full.
        if len(payload.get("daily_activities", [])) < payload.get(
            "per_page", 30
        ):
            break

        params["page"] += 1

    return activities


# ---------------------------------------------------------------------------
# Baby Buddy
# ---------------------------------------------------------------------------

def babybuddy_get_all(endpoint, params=None):
    """
    Retrieve every page from a Baby Buddy collection endpoint.
    """

    url = f"{BABYBUDDY_URL}/api/{endpoint}/"
    params = dict(params or {})
    params.setdefault("limit", 100)

    results = []

    while url:
        response = babybuddy.get(
            url,
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()

        results.extend(payload.get("results", []))

        url = payload.get("next")
        params = {}

    return results


def babybuddy_post(endpoint, json=None, files=None, data=None):
    url = f"{BABYBUDDY_URL}/api/{endpoint}/"

    response = babybuddy.post(
        url,
        json=json,
        files=files,
        data=data,
        timeout=60,
    )

    if not response.ok:
        print(
            f"Baby Buddy POST {url} failed:\n"
            f"{response.status_code}: {response.text}",
            file=sys.stderr,
        )

    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def extract_procare_id(record):
    """
    Extract the Procare UUID from a Baby Buddy record's notes.
    """

    text = record.get("notes") or record.get("note") or ""

    match = PROCARE_ID_RE.search(text)

    return match.group(1) if match else None


def get_imported_ids():
    """
    Build a set of all Procare IDs already present in Baby Buddy's
    Daycare-tagged records.
    """

    imported = set()

    for endpoint in (
        "changes",
        "feedings",
        "sleep",
        "notes",
    ):
        try:
            records = babybuddy_get_all(
                endpoint,
                {"tags": BABYBUDDY_TAG},
            )
        except requests.HTTPError as e:
            print(
                f"Warning: couldn't query {endpoint} by tag: {e}",
                file=sys.stderr,
            )
            continue

        for record in records:
            procare_id = extract_procare_id(record)

            if procare_id:
                imported.add(procare_id)

    return imported


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def parse_time(value):
    return datetime.fromisoformat(value)


def source_note(activity):
    """
    Generate the persistent metadata that makes the import idempotent.
    """

    parts = [
        f"[Procare ID: {activity['id']}]",
    ]

    data = activity.get("data") or {}

    if data.get("desc"):
        parts.append(data["desc"].strip())

    if activity.get("comment"):
        parts.append(activity["comment"].strip())

    if activity.get("staff_present_name"):
        parts.append(
            f"Room: {activity['staff_present_name']}"
        )

    return "\n".join(parts)


def convert_activity(activity):
    """
    Return:

        (endpoint, JSON body)

    or None for activities that should not be imported.
    """

    activity_type = activity["activity_type"]
    data = activity.get("data") or {}

    activity_time = parse_time(activity["activity_time"])
    notes = source_note(activity)

    # ---------------------------------------------------------------
    # Diaper
    # ---------------------------------------------------------------

    if activity_type == "bathroom_activity":
        subtype = data.get("sub_type", "").lower()

        return "changes", {
            "child": BABYBUDDY_CHILD_ID,
            "time": activity_time.isoformat(),
            "wet": "wet" in subtype,
            "solid": "bm" in subtype or "solid" in subtype,
            "tags": [BABYBUDDY_TAG],
            "note": notes,
        }

    # ---------------------------------------------------------------
    # Bottle
    # ---------------------------------------------------------------

    if activity_type == "bottle_activity":
        amount = data.get("bottle_consumed") or data.get("amount")

        if amount is None:
            print(
                f"Warning: bottle activity {activity['id']} "
                "has no amount; skipping.",
                file=sys.stderr,
            )
            return None

        return "feedings", {
            "child": BABYBUDDY_CHILD_ID,
            "start": activity_time.isoformat(),
            "end": activity_time.isoformat(),
            "type": "formula",
            "method": "bottle",
            "amount": int(float(amount)),
            "tags": [BABYBUDDY_TAG],
            "note": f"{notes}\nAmount: {amount} oz",
        }

    # ---------------------------------------------------------------
    # Nap
    # ---------------------------------------------------------------

    if activity_type == "nap_activity":
        start = data.get("start_time")
        end = data.get("end_time")

        # Procare reports an open nap by leaving end_time empty.
        # Don't import it yet. The same Procare UUID will eventually
        # be returned with the completed end time.
        if not start or not end:
            print(
                f"SKIP open nap {activity['id']}",
            )
            return None

        return "sleep", {
            "child": BABYBUDDY_CHILD_ID,
            "start": start,
            "end": end,
            "nap": True,
            "tags": [BABYBUDDY_TAG],
            "note": notes,
        }

    # ---------------------------------------------------------------
    # Photo
    # ---------------------------------------------------------------

    if activity_type == "photo_activity":
        return "notes", {
            "child": BABYBUDDY_CHILD_ID,
            "time": activity_time.isoformat(),
            "tags": [BABYBUDDY_TAG],
            "note": notes,
            "_photo_url": activity.get("photo_url"),
            "_photo_caption": (
                activity.get("activiable", {}).get("caption")
                or activity.get("comment")
                or "Daycare photo"
            ),
        }

    # ---------------------------------------------------------------
    # Explicitly ignored
    # ---------------------------------------------------------------

    if activity_type == "sign_in_activity":
        return None

    print(
        f"SKIP unsupported activity type: {activity_type}",
        file=sys.stderr,
    )

    return None


# ---------------------------------------------------------------------------
# Photo upload
# ---------------------------------------------------------------------------

def create_photo_note(body):
    """
    Download the Procare signed URL and upload the image to Baby Buddy
    as a multipart Note request.
    """

    photo_url = body.pop("_photo_url", None)
    caption = body.pop("_photo_caption", None)

    if not photo_url:
        return babybuddy_post("notes", json=body)

    print("  Downloading photo...")

    response = procare.get(
        photo_url,
        timeout=60,
    )
    response.raise_for_status()

    content_type = (
        response.headers.get("Content-Type")
        or "image/jpeg"
    )

    extension = ".jpg"

    if "png" in content_type:
        extension = ".png"
    elif "webp" in content_type:
        extension = ".webp"

    filename = f"procare-{body['child']}-{body['time']}{extension}"

    form = {
        "child": str(body["child"]),
        "time": body["time"],
        "tags": BABYBUDDY_TAG,
        "note": f"{body['note']}\n{caption.strip()}",
    }

    files = {
        "image": (
            filename,
            response.content,
            content_type,
        )
    }

    return babybuddy_post(
        "notes",
        data=form,
        files=files,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_day(day, dry_run=False):
    print(f"Fetching Procare activities for {day}...")

    activities = fetch_procare(day)

    print(f"Found {len(activities)} Procare activities.")

    print("Checking Baby Buddy for existing Procare IDs...")

    imported_ids = get_imported_ids()

    print(
        f"Found {len(imported_ids)} previously imported Procare activities."
    )

    # Oldest first.
    activities.sort(
        key=lambda activity: activity.get("activity_time", "")
    )

    imported = 0
    duplicates = 0
    skipped = 0

    for activity in activities:
        procare_id = activity["id"]
        activity_type = activity["activity_type"]
        activity_time = activity["activity_time"]

        if procare_id in imported_ids:
            print(
                f"SKIP duplicate {activity_type:20} "
                f"{activity_time}"
            )
            duplicates += 1
            continue

        converted = convert_activity(activity)

        if converted is None:
            skipped += 1
            continue

        endpoint, body = converted

        if dry_run:
            print(
                f"DRY RUN {activity_type:20} "
                f"{activity_time} -> {endpoint}"
            )
            continue

        print(
            f"IMPORT   {activity_type:20} "
            f"{activity_time} -> {endpoint}"
        )

        if endpoint == "notes" and "_photo_url" in body:
            result = create_photo_note(body)
        else:
            result = babybuddy_post(endpoint, json=body)

        babybuddy_id = result.get("id")

        print(f"         Baby Buddy ID: {babybuddy_id}")

        # Important: update our in-memory set immediately. This protects
        # against duplicate Procare records in the same response.
        imported_ids.add(procare_id)

        imported += 1

    print()
    print(f"Imported:   {imported}")
    print(f"Duplicate:  {duplicates}")
    print(f"Skipped:    {skipped}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "date",
        nargs="?",
        default=date.today().isoformat(),
        help="Date to import (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        parser.error("Date must be YYYY-MM-DD")

    import_day(
        day,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
