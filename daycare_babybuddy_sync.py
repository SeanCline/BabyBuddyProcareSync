#!/usr/bin/env python3

"""
Import a day of Procare daycare activities into Baby Buddy.

    daycare_babybuddy_sync.py                       # today
    daycare_babybuddy_sync.py 2026-08-17 --dry-run
    daycare_babybuddy_sync.py --from-file activity.json --dry-run
    daycare_babybuddy_sync.py --login               # force a fresh sign-in

Procare credentials are read from $PROCARE_EMAIL / $PROCARE_PASSWORD, or
prompted for interactively. The resulting session token is cached so that
routine runs need no credentials at all.
"""

import argparse
import getpass
import json
import os
import re
import sys
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROCARE_API = os.environ.get("PROCARE_API", "https://api-school.procareconnect.com/api/web/")
PROCARE_EMAIL = os.environ.get("PROCARE_EMAIL")
PROCARE_PASSWORD = os.environ.get("PROCARE_PASSWORD")
PROCARE_TOKEN = os.environ.get("PROCARE_TOKEN")
PROCARE_KID_ID = os.environ.get("PROCARE_KID_ID")

SCRIPT_DIR = Path(__file__).resolve().parent

# Kept beside the script by default: in a container the script directory is the bind mount, so the session survives the container being recreated.
TOKEN_CACHE = Path(os.environ.get("PROCARE_TOKEN_CACHE") or SCRIPT_DIR / ".procare-token.json")

BABYBUDDY_URL = os.environ.get("BABYBUDDY_URL").rstrip("/")

BABYBUDDY_TOKEN = os.environ.get("BABYBUDDY_TOKEN", "")
BABYBUDDY_CHILD_ID = int(os.environ.get("BABYBUDDY_CHILD_ID"))
BABYBUDDY_TAG = os.environ.get("BABYBUDDY_TAG", "daycare")

# Bottles at daycare are whatever we send in; Procare rarely fills in data.bottle_type, so this is the fallback.
BOTTLE_TYPE = os.environ.get("BABYBUDDY_BOTTLE_TYPE", "breast milk")

# A merged nap longer than this is almost certainly a mis-paired start/end rather than a real sleep, so it is reported instead of stored.
MAX_NAP = timedelta(hours=float(os.environ.get("MAX_NAP_HOURS", "6")))

# Baby Buddy endpoints that hold the imported records, and the Procare UUID marker that makes re-running the import a no-op.
IMPORT_ENDPOINTS = ("changes", "feedings", "sleep", "notes")
PROCARE_ID_RE = re.compile(r"\[Procare ID: ([0-9a-fA-F-]{36})\]")


def log(message):
    print(message)


def warn(message):
    print(f"WARN     {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Procare
# ---------------------------------------------------------------------------

class ProcareClient:
    """
    Read-only access to the Procare parent API.

    Authentication is lazy: nothing is requested until an endpoint that
    needs a token is called, which keeps --from-file runs offline.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 Procare-BabyBuddy-Importer",
            "Accept": "application/json",
            "Origin": "https://schools.procareconnect.com",
            "Referer": "https://schools.procareconnect.com/",
        })

        self._token = None
        self._kid_id = PROCARE_KID_ID

        # Only re-authenticate mid-run if we can get credentials without
        # a human watching, or there is a human watching.
        self._can_login = bool(PROCARE_EMAIL and PROCARE_PASSWORD) or (
            sys.stdin is not None and sys.stdin.isatty()
        )

    # -- authentication ------------------------------------------------

    def _use_token(self, token, source):
        log(f"Procare session token from {source}.")

        self._token = token
        self.session.headers["Authorization"] = f"Bearer {token}"

    def sign_in(self):
        """
        Sign in now, replacing whatever token was cached.
        """

        self._use_token(self._login(), "sign-in")

    def _authenticate(self):
        if PROCARE_TOKEN:
            self._use_token(PROCARE_TOKEN, "$PROCARE_TOKEN")
            return

        cached = read_cached_token()

        if cached:
            self._use_token(cached, str(TOKEN_CACHE))
            return

        self._use_token(self._login(), "sign-in")

    def _login(self):
        email = PROCARE_EMAIL
        password = PROCARE_PASSWORD

        if not (email and password):
            if not self._can_login:
                sys.exit(
                    "Procare needs credentials: set $PROCARE_EMAIL and "
                    "$PROCARE_PASSWORD, or run interactively to sign in."
                )

            log("Signing in to Procare.")
            email = email or input("Procare email: ").strip()
            password = password or getpass.getpass("Procare password: ")

        response = self.session.post(
            f"{PROCARE_API}auth/",
            json={"email": email, "password": password},
            timeout=30,
        )

        payload = response_json(response)
        errors = payload.get("errors") if isinstance(payload, dict) else None

        if errors:
            sys.exit(f"Procare sign-in failed: {'; '.join(errors)}")

        response.raise_for_status()

        # The token lives under "user" today; accept a top-level one too
        # so a shuffled response shape doesn't break sign-in.
        token = payload.get("auth_token") or (
            payload.get("user") or {}
        ).get("auth_token")

        if not token:
            sys.exit(
                "Procare sign-in succeeded but no auth_token was returned; "
                f"response keys: {sorted(payload)}"
            )

        write_cached_token(token)

        return token

    # -- requests -----------------------------------------------------

    def get(self, path, params=None):
        if self._token is None:
            self._authenticate()

        for attempt in (1, 2):
            response = self.session.get(
                f"{PROCARE_API}{path}",
                params=params,
                timeout=30,
            )

            if response.status_code not in (401, 403):
                response.raise_for_status()
                return response.json()

            if attempt == 2 or not self._can_login:
                sys.exit(
                    "Procare rejected the session token. Set "
                    "$PROCARE_EMAIL/$PROCARE_PASSWORD or run interactively "
                    "to sign in again."
                )

            warn("Procare session expired; signing in again.")
            self._use_token(self._login(), "sign-in")

    def download(self, url):
        """
        Fetch a Procare-hosted file. Photo URLs are pre-signed, so this
        deliberately does not require a session token.
        """

        response = self.session.get(url, timeout=60)
        response.raise_for_status()

        return response.content, response.headers.get("Content-Type", "")

    @property
    def kid_id(self):
        if self._kid_id is None:
            self._kid_id = self._only_kid()

        return self._kid_id

    def _only_kid(self):
        kids = self.get("parent/kids/").get("kids", [])

        if len(kids) == 1:
            log(f"Importing for {kids[0]['name']} ({kids[0]['id']}).")
            return kids[0]["id"]

        listing = "\n".join(
            f"  {kid['id']}  {kid.get('name')}" for kid in kids
        )

        sys.exit(
            "Set $PROCARE_KID_ID to the child to import; the account has "
            f"{len(kids)} children:\n{listing}"
        )

    def activities(self, day):
        """
        Every daily activity Procare recorded for a single day.
        """

        params = {
            "kid_id": self.kid_id,
            # date_from matters: with only date_to, Procare returns
            # everything up to that date.
            "filters[daily_activity][date_from]": day.isoformat(),
            "filters[daily_activity][date_to]": day.isoformat(),
            "page": 1,
        }

        activities = []

        while True:
            payload = self.get("parent/daily_activities/", params)
            page = payload.get("daily_activities", [])

            activities.extend(page)

            if len(page) < payload.get("per_page", 30):
                return activities

            params["page"] += 1


def read_cached_token():
    try:
        return json.loads(TOKEN_CACHE.read_text())["token"]
    except (OSError, ValueError, KeyError):
        return None


def write_cached_token(token):
    payload = {
        "token": token,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        TOKEN_CACHE.write_text(json.dumps(payload, indent=2))
    except OSError as e:
        warn(f"couldn't cache the Procare token in {TOKEN_CACHE}: {e}")
        return

    # Best effort; a no-op on Windows.
    with suppress(OSError):
        TOKEN_CACHE.chmod(0o600)

    log(f"Cached Procare session token in {TOKEN_CACHE}.")


def response_json(response):
    try:
        return response.json()
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# Baby Buddy
# ---------------------------------------------------------------------------

class BabyBuddyClient:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {BABYBUDDY_TOKEN}",
            "Accept": "application/json",
        })

    def get_all(self, endpoint, params=None):
        """
        Every page of a Baby Buddy collection endpoint.
        """

        url = f"{BABYBUDDY_URL}/api/{endpoint}/"
        params = {"limit": 100, **(params or {})}

        results = []

        while url:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()

            payload = response.json()
            results.extend(payload.get("results", []))

            url = payload.get("next")
            params = None  # `next` already carries the query string.

        return results

    def post(self, endpoint, **kwargs):
        url = f"{BABYBUDDY_URL}/api/{endpoint}/"

        response = self.session.post(url, timeout=60, **kwargs)

        if not response.ok:
            warn(
                f"Baby Buddy POST {url} failed: "
                f"{response.status_code}: {response.text}"
            )

        response.raise_for_status()

        return response.json()

    def imported_procare_ids(self):
        """
        The Procare UUIDs already recorded in Baby Buddy, read back out of
        the notes of tagged records.
        """

        imported = set()

        for endpoint in IMPORT_ENDPOINTS:
            try:
                records = self.get_all(endpoint, {"tags": BABYBUDDY_TAG})
            except requests.HTTPError as e:
                warn(f"couldn't query {endpoint} by tag: {e}")
                continue

            for record in records:
                text = record.get("notes") or record.get("note") or ""
                imported.update(PROCARE_ID_RE.findall(text))

        return imported

    def create(self, entry, procare):
        """
        Store one planned entry, uploading its photo if it has one.
        """

        if not entry.photo_url:
            return self.post(entry.endpoint, json=entry.payload())

        log("         downloading photo...")

        content, content_type = procare.download(entry.photo_url)
        extension = image_extension(content_type)

        # Multipart, so every value has to be a string (or a list of them).
        form = {
            key: value if isinstance(value, list) else str(value)
            for key, value in entry.payload().items()
        }

        return self.post(
            entry.endpoint,
            data=form,
            files={
                "image": (
                    f"procare-{entry.procare_ids[0]}{extension}",
                    content,
                    content_type or "image/jpeg",
                )
            },
        )


def image_extension(content_type):
    for candidate in (".png", ".webp", ".gif"):
        if candidate.strip(".") in (content_type or ""):
            return candidate

    return ".jpg"


# ---------------------------------------------------------------------------
# Planned entries
# ---------------------------------------------------------------------------

# Baby Buddy calls the free-text field "note" on the notes endpoint and
# "notes" everywhere else; sending the wrong one is silently ignored.
NOTE_FIELD = {"notes": "note"}


@dataclass
class Entry:
    """
    One Baby Buddy record to create, and the Procare activities it came
    from. A nap covers two Procare activities; everything else covers one.
    """

    endpoint: str
    activity_type: str
    time: str
    procare_ids: tuple
    note: str
    fields: dict = field(default_factory=dict)
    photo_url: str = None

    def payload(self):
        return {
            "child": BABYBUDDY_CHILD_ID,
            "tags": [BABYBUDDY_TAG],
            NOTE_FIELD.get(self.endpoint, "notes"): self.note,
            **self.fields,
        }

    def __str__(self):
        return f"{self.activity_type:20} {self.time} -> {self.endpoint}"


def parse_time(value):
    return datetime.fromisoformat(value)


def build_note(activities, *fragments):
    """
    The note body: the Procare UUIDs first, because duplicate detection
    reads them back out of Baby Buddy, then whatever context was passed in.

    Procare repeats itself a lot - a photo's caption also arrives as the
    activity comment - so identical fragments are only kept once.
    """

    lines = []

    for fragment in [f"[Procare ID: {a['id']}]" for a in activities] + list(fragments):
        text = (fragment or "").strip()

        if text and text not in lines:
            lines.append(text)

    return "\n".join(lines)


def context_of(activity, staff_label="Staff"):
    """
    The human-readable bits Procare attaches to most activities.
    """

    data = activity.get("data") or {}
    room = activity.get("staff_present_name")

    return [
        # data.desc is the initials of the staff member who logged it.
        f"{staff_label}: {data['desc'].strip()}" if data.get("desc") else None,
        f"Room: {room}" if room else None,
        activity.get("comment"),
    ]


def convert_diaper(activity):
    data = activity["data"]
    sub_type = (data.get("sub_type") or "").lower()

    return Entry(
        endpoint="changes",
        activity_type=activity["activity_type"],
        time=activity["activity_time"],
        procare_ids=(activity["id"],),
        note=build_note(
            [activity],
            *context_of(activity),
            "Diaper cream applied" if data.get("diaper_cream") else None,
        ),
        fields={
            "time": activity["activity_time"],
            "wet": "wet" in sub_type,
            "solid": "bm" in sub_type or "solid" in sub_type,
        },
    )


def convert_bottle(activity):
    data = activity["data"]
    amount = data.get("bottle_consumed") or data.get("amount")

    if amount is None or amount == "":
        warn(f"bottle {activity['id']} has no amount; skipping.")
        return None

    bottle_type = data.get("bottle_type") or ""

    return Entry(
        endpoint="feedings",
        activity_type=activity["activity_type"],
        time=activity["activity_time"],
        procare_ids=(activity["id"],),
        note=build_note([activity], *context_of(activity)),
        fields={
            # Procare records when the bottle was finished, not a range.
            "start": activity["activity_time"],
            "end": activity["activity_time"],
            "type": "breast milk" if "breast" in bottle_type.lower() else BOTTLE_TYPE,
            "method": "bottle",
            "amount": float(amount),
        },
    )


def convert_photo(activity):
    caption = (activity.get("activiable") or {}).get("caption")

    return Entry(
        endpoint="notes",
        activity_type=activity["activity_type"],
        time=activity["activity_time"],
        procare_ids=(activity["id"],),
        note=build_note(
            [activity],
            *context_of(activity),
            caption or "Daycare photo",
        ),
        fields={"time": activity["activity_time"]},
        photo_url=activity.get("photo_url"),
    )


def convert_sign_in_out(activity):
    """
    Sign-in and sign-out both become plain notes. Procare keeps them as
    two activities over one shared attendance record, so each is imported
    from its own side of that record.
    """

    attendance = activity.get("activiable") or {}
    signing_out = activity["activity_type"] == "sign_out_activity"

    action = "Signed out" if signing_out else "Signed in"
    who = attendance.get("signed_out_by" if signing_out else "signed_in_by")
    section = (attendance.get("section") or {}).get("name")

    return Entry(
        endpoint="notes",
        activity_type=activity["activity_type"],
        time=activity["activity_time"],
        procare_ids=(activity["id"],),
        note=build_note(
            [activity],
            f"{action} by {who}" if who else action,
            f"Room: {section}" if section else None,
            attendance.get("note"),
            activity.get("comment"),
        ),
        fields={"time": activity["activity_time"]},
    )


CONVERTERS = {
    "bathroom_activity": convert_diaper,
    "bottle_activity": convert_bottle,
    "photo_activity": convert_photo,
    "sign_in_activity": convert_sign_in_out,
    "sign_out_activity": convert_sign_in_out,
    # nap_activity is handled by pair_naps(), which spans two activities.
}


# ---------------------------------------------------------------------------
# Naps
# ---------------------------------------------------------------------------

def nap_entry(activities, start, end):
    """
    A sleep entry from a start/end pair, or None if the pair can't be
    trusted.
    """

    start_time = parse_time(start)
    end_time = parse_time(end)
    duration = end_time - start_time

    if duration <= timedelta(0):
        warn(
            f"nap ends at or before it starts ({start} -> {end}); "
            "skipping."
        )
        return None

    if duration > MAX_NAP:
        warn(
            f"nap of {duration} ({start} -> {end}) is longer than "
            f"{MAX_NAP} and looks mis-paired; skipping. Raise "
            "$MAX_NAP_HOURS if it is real."
        )
        return None

    # One record per side of the nap, so name the staff on each side.
    labels = ["Down", "Up"] if len(activities) == 2 else ["Staff"]

    return Entry(
        endpoint="sleep",
        activity_type="nap_activity",
        time=start,
        procare_ids=tuple(a["id"] for a in activities),
        note=build_note(
            activities,
            *[
                fragment
                for activity, label in zip(activities, labels)
                for fragment in context_of(activity, label)
            ],
        ),
        fields={"start": start, "end": end, "nap": True},
    )


def pair_naps(naps):
    """
    Yield one sleep entry per nap.

    Procare logs a nap as two separate activities: one carrying
    data.start_time and, later, one carrying data.end_time. They share no
    id, batch or index, so the only thing linking them is the clock - an
    end closes the most recent still-open start of the same day.

    A start with no end yet is left alone. Procare will report the wake-up
    as a new activity, and a later run pairs the two then.
    """

    open_nap = None

    for nap in sorted(naps, key=lambda a: a.get("activity_time", "")):
        data = nap.get("data") or {}
        start = data.get("start_time")
        end = data.get("end_time")

        if start and end:
            # Not seen in the wild, but a self-contained nap needs no
            # pairing.
            entry = nap_entry([nap], start, end)
        elif start:
            if open_nap:
                warn(
                    f"nap {open_nap['id']} started at "
                    f"{open_nap['data']['start_time']} was never closed; "
                    "ignoring it in favour of the later start."
                )

            open_nap = nap
            continue
        elif end:
            if open_nap is None:
                warn(
                    f"nap {nap['id']} ends at {end} with no start "
                    "logged that day; skipping."
                )
                continue

            entry = nap_entry(
                [open_nap, nap], open_nap["data"]["start_time"], end
            )
            open_nap = None
        else:
            warn(f"nap {nap['id']} has neither a start nor an end; skipping.")
            continue

        if entry:
            yield entry

    if open_nap:
        log(
            f"WAIT     nap_activity         "
            f"{open_nap['data']['start_time']} still in progress; will "
            "import once daycare logs the wake-up."
        )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def plan(activities):
    """
    Turn Procare activities into the Baby Buddy records to create, oldest
    first.
    """

    entries = []
    naps_by_day = defaultdict(list)

    for activity in activities:
        activity_type = activity["activity_type"]

        if activity_type == "nap_activity":
            # Naps are paired per day, so gather them first.
            naps_by_day[activity.get("activity_date")].append(activity)
            continue

        convert = CONVERTERS.get(activity_type)

        if convert is None:
            warn(
                f"unsupported activity type {activity_type} "
                f"({activity['id']}); skipping."
            )
            continue

        entry = convert(activity)

        if entry:
            entries.append(entry)

    for day in sorted(naps_by_day):
        entries.extend(pair_naps(naps_by_day[day]))

    entries.sort(key=lambda entry: entry.time)

    return entries


def sync(activities, procare, dry_run=False):
    babybuddy = BabyBuddyClient()

    entries = plan(activities)

    log(
        f"Planned {len(entries)} Baby Buddy records from "
        f"{len(activities)} Procare activities."
    )

    known_ids = babybuddy.imported_procare_ids()

    log(f"Baby Buddy already holds {len(known_ids)} imported activities.")

    imported = 0
    duplicates = 0

    for entry in entries:
        if known_ids.intersection(entry.procare_ids):
            log(f"SKIP dup {entry}")
            duplicates += 1
            continue

        if dry_run:
            log(f"DRY RUN  {entry}")
            log(describe(entry))
            continue

        log(f"IMPORT   {entry}")

        result = babybuddy.create(entry, procare)

        log(f"         Baby Buddy ID: {result.get('id')}")

        # Update immediately: two Procare activities in one response can
        # merge into a single entry, and re-runs shouldn't depend on
        # re-reading Baby Buddy.
        known_ids.update(entry.procare_ids)

        imported += 1

    log("")
    log(f"Imported:   {imported}")
    log(f"Duplicate:  {duplicates}")


def describe(entry):
    """
    A compact, one-record-per-line view of a --dry-run entry.
    """

    payload = entry.payload()

    # The first note line is the Procare ID, which the log line already
    # implies; the rest is the interesting part.
    note = payload.pop(NOTE_FIELD.get(entry.endpoint, "notes"), "")
    context = " | ".join(note.splitlines()[len(entry.procare_ids):])

    fields = " ".join(
        f"{key}={value}"
        for key, value in payload.items()
        if key not in ("child", "tags")
    )

    if entry.photo_url:
        fields += " photo=yes"

    return f"         {fields}" + (f"\n         {context}" if context else "")


def load_file(path, day):
    payload = json.loads(Path(path).read_text())
    activities = payload.get("daily_activities", [])

    if day:
        activities = [
            a for a in activities if a.get("activity_date") == day.isoformat()
        ]

    dates = sorted({a.get("activity_date") for a in activities})

    log(f"Read {len(activities)} activities from {path} ({', '.join(dates)}).")

    return activities


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "date",
        nargs="?",
        help="Date to import (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing to Baby Buddy.",
    )
    parser.add_argument(
        "--from-file",
        metavar="PATH",
        help="Read a saved Procare response instead of calling the API.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Sign in to Procare again, replacing the cached token.",
    )

    args = parser.parse_args()

    day = None

    if args.date:
        try:
            day = date.fromisoformat(args.date)
        except ValueError:
            parser.error("Date must be YYYY-MM-DD")

    procare = ProcareClient()

    if args.login:
        procare.sign_in()

    if args.from_file:
        activities = load_file(args.from_file, day)
    else:
        day = day or date.today()

        log(f"Fetching Procare activities for {day}...")
        activities = procare.activities(day)

        log(f"Found {len(activities)} Procare activities.")

    sync(activities, procare, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
