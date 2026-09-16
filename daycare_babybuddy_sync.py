#!/usr/bin/env python3

"""
Import Procare daycare activities into Baby Buddy.

    daycare_babybuddy_sync.py                       # today and yesterday
    daycare_babybuddy_sync.py 2026-08-17 --dry-run  # one day
    daycare_babybuddy_sync.py --start 2026-08-10 --end 2026-08-17
    daycare_babybuddy_sync.py --days-back 7         # the last week
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

# The app's own API. The parallel /api/web/ namespace serves the same activities but rejects tokens from the sign-in below, which is the one that can register for push.
PROCARE_API = os.environ.get("PROCARE_API", "https://api-school.procareconnect.com/api/mobile/")

# Sign-in lives on its own host, and hands back a token the API above accepts.
PROCARE_AUTH_API = os.environ.get("PROCARE_AUTH_API", "https://online-auth.procareconnect.com/")
PROCARE_EMAIL = os.environ.get("PROCARE_EMAIL")
PROCARE_PASSWORD = os.environ.get("PROCARE_PASSWORD")
PROCARE_TOKEN = os.environ.get("PROCARE_TOKEN")
PROCARE_KID_ID = os.environ.get("PROCARE_KID_ID")

SCRIPT_DIR = Path(__file__).resolve().parent

# Kept beside the script by default: in a container the script directory is the bind mount, so the session survives the container being recreated.
TOKEN_CACHE = Path(os.environ.get("PROCARE_TOKEN_CACHE") or SCRIPT_DIR / ".procare-token.json")

# Baby Buddy has nowhere to keep a video, and Procare's links to them are signed and expire within months, so the files are pulled down to here instead.
VIDEO_DIR = Path(os.environ.get("PROCARE_VIDEO_DIR") or SCRIPT_DIR / "videos")

BABYBUDDY_URL = os.environ.get("BABYBUDDY_URL").rstrip("/")

BABYBUDDY_TOKEN = os.environ.get("BABYBUDDY_TOKEN", "")
BABYBUDDY_CHILD_ID = int(os.environ.get("BABYBUDDY_CHILD_ID"))
BABYBUDDY_TAG = os.environ.get("BABYBUDDY_TAG", "daycare")

# Bottles at daycare are whatever we send in; Procare rarely fills in data.bottle_type, so this is the fallback.
BOTTLE_TYPE = os.environ.get("BABYBUDDY_BOTTLE_TYPE", "breast milk")

# The window to import. Dates may be YYYY-MM-DD or the words "today" and "yesterday"; SYNC_DAYS_BACK fills in a start that was not given.
SYNC_START_DATE = os.environ.get("SYNC_START_DATE")
SYNC_END_DATE = os.environ.get("SYNC_END_DATE")

# One day of history by default: daycare keeps logging after the last run of the evening, and a nap that ends after midnight belongs to the day before.
SYNC_DAYS_BACK = int(os.environ.get("SYNC_DAYS_BACK") or 1)

# A merged nap longer than this is almost certainly a mis-paired start/end rather than a real sleep, so it is reported instead of stored.
MAX_NAP = timedelta(hours=float(os.environ.get("MAX_NAP_HOURS", "6")))

# Baby Buddy endpoints that hold the imported records, and the Procare UUID marker that makes re-running the import a no-op.
IMPORT_ENDPOINTS = ("changes", "feedings", "sleep", "notes")
PROCARE_ID_RE = re.compile(r"\[Procare ID: ([0-9a-fA-F-]{36})\]")


# Procare's notes and notification text carry emoji, which a Windows console in its default code page raises on rather than prints.
for _stream in (sys.stdout, sys.stderr):
    with suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")


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

    def __init__(self, device=None):
        # A push device (from procare_fcm) subscribes every sign-in this
        # client makes to Procare's notifications.
        self.device = device

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
        """
        Exchange credentials for a session token.

        Signing in with a push device subscribes the session to Procare's
        notifications, which is exactly what the phone app does here.
        """

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

        credentials = {
            "email": email,
            "password": password,
            # What the app sends: a parent signing in from an Android phone.
            "role": "carer",
            "platform": "android",
        }

        if self.device:
            credentials["fcm_token"] = self.device["token"]
            credentials["device_token"] = self.device["android_id"]

        response = self.session.post(
            f"{PROCARE_AUTH_API}sessions",
            json=credentials,
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

        write_cached_token(token, self.device["token"] if self.device else None)

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

    def activities(self, start, end):
        """
        Every daily activity Procare recorded between two dates, inclusive.
        """

        params = {
            "kid_id": self.kid_id,
            # date_from matters: with only date_to, Procare returns
            # everything up to that date.
            "filters[daily_activity][date_from]": start.isoformat(),
            "filters[daily_activity][date_to]": end.isoformat(),
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


def read_cached_session():
    try:
        return json.loads(TOKEN_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def read_cached_token():
    return read_cached_session().get("token")


def cached_session_subscribes(device):
    """
    Whether the cached session is already receiving this device's pushes.

    Push subscription is made at sign-in and belongs to the session, so a
    restart only has to sign in again when the session is gone or was
    signed in with some other device.
    """

    cached = read_cached_session()

    return bool(cached.get("token")) and cached.get("fcm_token") == device["token"]


def write_cached_token(token, fcm_token=None):
    payload = {
        "token": token,
        "fcm_token": fcm_token,
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


# Procare serves whatever the phone recorded; anything unrecognised is far
# more likely to be MP4 than not.
VIDEO_EXTENSIONS = {
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "video/x-matroska": ".mkv",
}


def archive_video(entry, procare):
    """
    Save an entry's video to VIDEO_DIR and return the note line naming it.

    Baby Buddy only stores the still frame, so this file is the only copy
    of the video that outlives Procare's signed link - and if it can't be
    fetched, that link still beats recording nothing.
    """

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    stem = f"procare-{entry.procare_ids[0]}"
    existing = next(VIDEO_DIR.glob(f"{stem}.*"), None)

    if existing:
        return f"Video: {existing.name}"

    log("         downloading video...")

    try:
        content, content_type = procare.download(entry.video_url)
    except requests.RequestException as error:
        warn(f"could not download video {entry.procare_ids[0]}: {error}")
        return f"Video: {entry.video_url}"

    media_type = (content_type or "").split(";")[0].strip().lower()
    path = VIDEO_DIR / f"{stem}{VIDEO_EXTENSIONS.get(media_type, '.mp4')}"

    path.write_bytes(content)

    log(f"         saved {path.name} ({len(content) // 1024} KiB)")

    return f"Video: {path.name}"


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
    video_url: str = None

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


def convert_media(activity):
    """
    Photos and videos both become notes. Procare hands back a still frame
    for either, so that is what gets attached to the note; a video's own
    file is saved to VIDEO_DIR by archive_video().
    """

    media = activity.get("activiable") or {}
    is_video = activity["activity_type"] == "video_activity"
    video_url = media.get("video_file_url") if is_video else None

    return Entry(
        endpoint="notes",
        activity_type=activity["activity_type"],
        time=activity["activity_time"],
        procare_ids=(activity["id"],),
        note=build_note(
            [activity],
            *context_of(activity),
            media.get("caption") or ("Daycare video" if is_video else "Daycare photo"),
        ),
        fields={"time": activity["activity_time"]},
        photo_url=activity.get("photo_url"),
        video_url=video_url,
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
    "photo_activity": convert_media,
    "video_activity": convert_media,
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

    # Videos are archived for every entry, not just the new ones: a note
    # that already exists can still be missing its video, from a download
    # that failed on an earlier run, and the Procare link it came from
    # expires. Files already on disk are left alone.
    if not dry_run:
        for entry in entries:
            if entry.video_url:
                entry.note = build_note(
                    [], entry.note, archive_video(entry, procare)
                )

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


def import_window(procare, start, end, dry_run=False):
    """
    Fetch a window of Procare activities and import them.
    """

    window = start if start == end else f"{start} to {end}"

    log(f"Fetching Procare activities for {window}...")
    activities = procare.activities(start, end)

    log(f"Found {len(activities)} Procare activities.")

    sync(activities, procare, dry_run=dry_run)


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

    if entry.video_url:
        fields += " video=yes"

    return f"         {fields}" + (f"\n         {context}" if context else "")


def parse_day(value):
    """
    A date written as YYYY-MM-DD, or as "today" or "yesterday".
    """

    text = value.strip().lower()

    if text == "today":
        return date.today()

    if text == "yesterday":
        return date.today() - timedelta(days=1)

    return date.fromisoformat(text)


def sync_range(start=None, end=None, days_back=None):
    """
    The dates to import, inclusive, from whichever end was pinned down.

    A start date wins where it is given; otherwise the window is that many
    days of history ending at the end date, which leaves the ordinary case
    - the last day or two, up to today - needing no dates at all.
    """

    end = end or parse_day(SYNC_END_DATE or "today")
    start = start or (parse_day(SYNC_START_DATE) if SYNC_START_DATE else None)

    if start and days_back is not None:
        warn("a start date and a day count were both given; using the start date.")

    if not start:
        start = end - timedelta(days=SYNC_DAYS_BACK if days_back is None else days_back)

    if start > end:
        sys.exit(f"Start date {start} is after end date {end}.")

    return start, end


def load_file(path, window=None):
    payload = json.loads(Path(path).read_text())
    activities = payload.get("daily_activities", [])

    if window:
        start, end = window
        activities = [
            a
            for a in activities
            if start.isoformat() <= (a.get("activity_date") or "") <= end.isoformat()
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
        help="A single date to import, short for --start DATE --end DATE.",
    )
    parser.add_argument(
        "--start",
        metavar="DATE",
        help="First date to import. Defaults to --days-back before the end.",
    )
    parser.add_argument(
        "--end",
        metavar="DATE",
        help="Last date to import. Defaults to today.",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        metavar="N",
        help=f"Days of history to import before the end date (default {SYNC_DAYS_BACK}).",
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

    if args.date and (args.start or args.end):
        parser.error("Give a single date or --start/--end, not both.")

    try:
        start = parse_day(args.start or args.date) if (args.start or args.date) else None
        end = parse_day(args.end or args.date) if (args.end or args.date) else None
    except ValueError:
        parser.error("Dates must be YYYY-MM-DD, today or yesterday.")

    start, end = sync_range(start, end, args.days_back)

    procare = ProcareClient()

    if args.login:
        procare.sign_in()

    if args.from_file:
        # A saved response is read whole unless dates were asked for, so
        # that a fixture from any day is still usable for testing.
        pinned = bool(args.date or args.start or args.end)
        activities = load_file(args.from_file, (start, end) if pinned else None)
        sync(activities, procare, dry_run=args.dry_run)
    else:
        import_window(procare, start, end, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
