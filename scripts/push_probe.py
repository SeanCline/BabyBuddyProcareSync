#!/usr/bin/env python3

"""
Listen for Procare pushes and report whether they look the way we expect.

    scripts/push_probe.py                 # until interrupted
    scripts/push_probe.py --hours 9

A diagnostic, not part of the import: it signs in so this device is
subscribed, then prints and appends every notification to a log, flagging
anything that does not match the shape read out of the Procare APK. Run it
for a day to find out which activities actually push.

Do not run it while procare_watch.py is running - both would connect to
Firebase as the same device.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

# The modules below sit a directory up, so this runs the same however it
# was invoked: by path, from another directory, or as a container command.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import procare_fcm  # noqa: E402
from daycare_babybuddy_sync import (  # noqa: E402
    PROCARE_KID_ID,
    SCRIPT_DIR,
    ProcareClient,
    cached_session_subscribes,
    log,
    warn,
)

# The push client reports connection trouble through logging, and a service
# that reconnects silently is a service nobody can debug.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


LOG_FILE = Path(os.environ.get("PUSH_PROBE_LOG") or SCRIPT_DIR / "push-log.jsonl")

# What the app reads, plus what the server was seen to send. Procare sends
# these as notification messages, so every payload also carries Firebase's
# own gcm.notification.* and google.c.* plumbing, which is ignored below.
KNOWN_KEYS = {
    "body",
    "category",
    "coupon",
    "daily_activity_id",
    "kid_id",
    "message",
    "school_id",
    "section_id",
    "title",
    "uri",
}

# Activity types arrive as the category, which is how an import can tell what changed. The app names the rest.
KNOWN_CATEGORIES = {
    "bathroom_activity",
    "bottle_activity",
    "food_activity",
    "kid_note_activity",
    "learning_activity",
    "mood_activity",
    "nap_activity",
    "photo_activity",
    "video_activity",
    "event_created",
    "coupon",
    "message_general",
    "parent_admin_message",
    "staff_to_staff_message",
    "teacher_message_general",
    "admin_message_parent_admin",
    "parent_invoice",
    "promo",
    "sign_in_activity",
    "sign_out_activity",
    "subscribe",
    "teacher_sign_in_activity",
    "teacher_sign_out_activity",
    "geofence_reached",
}


def check(data):
    """
    Everything surprising about one notification.
    """

    notes = []
    category = data.get("category")

    if not category:
        notes.append("no category")
    elif category not in KNOWN_CATEGORIES:
        notes.append(f"category not seen in the APK: {category}")

    if not data.get("title"):
        notes.append("no title (the app would not show this one)")

    if not (data.get("body") or data.get("message")):
        notes.append("no body")

    plumbing = ("gcm.", "google.c.")
    extra = sorted(
        key for key in set(data) - KNOWN_KEYS if not key.startswith(plumbing)
    )

    if extra:
        notes.append(f"keys we have not seen before: {', '.join(extra)}")

    kid_id = data.get("kid_id")

    if kid_id and PROCARE_KID_ID and kid_id != PROCARE_KID_ID:
        notes.append(f"kid_id is not ours: {kid_id}")

    return notes


async def probe(seconds, login):
    device = procare_fcm.credentials()
    seen = Counter()

    def on_push(data, persistent_id, context):
        seen[data.get("category") or "?"] += 1

        log("")
        log(f"PUSH  {time.strftime('%H:%M:%S')}  {json.dumps(data, sort_keys=True)}")

        for note in check(data):
            warn(f"  {note}")

        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"received_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "data": data})
                + "\n"
            )

    # Signing in is what subscribes this device, but the subscription
    # belongs to the session, so a restart can just start listening.
    if login or not cached_session_subscribes(device):
        ProcareClient(device=device).sign_in()
    else:
        log("Reusing the cached session; it is already subscribed.")

    client = procare_fcm.client(device, on_push)
    await client.start()

    log(f"Listening as android id {device['android_id']}; logging to {LOG_FILE}.")

    try:
        await asyncio.sleep(seconds)
    finally:
        await client.stop()

        log("")
        log(f"Received {sum(seen.values())} notifications.")

        for category, count in seen.most_common():
            log(f"  {count:3} {category}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=float,
        default=24,
        help="How long to listen before stopping (default 24).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Sign in again, rather than reusing a cached session.",
    )

    args = parser.parse_args()

    try:
        asyncio.run(probe(args.hours * 3600, args.login))
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
