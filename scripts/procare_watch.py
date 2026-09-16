#!/usr/bin/env python3

"""
Watch Procare and import new activity as daycare logs it.

    scripts/procare_watch.py

Signs in to Procare with a push device, so this session receives the same
notifications the phone app does, and imports whenever one arrives. A push
is a hint rather than a payload: it says something happened, and the import
that follows is the ordinary one over the configured window.

Nothing is trusted to arrive. An import always runs at least every
$WATCH_POLL_MINUTES, so a dropped connection or a notification Procare
never sends costs a delay, not a missing record.
"""

import asyncio
import logging
import os
import sys

from pathlib import Path

# The modules below sit a directory up, so this runs the same however it
# was invoked: by path, from another directory, or as a container command.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import procare_fcm  # noqa: E402
from daycare_babybuddy_sync import (  # noqa: E402
    ProcareClient,
    cached_session_subscribes,
    import_window,
    log,
    sync_range,
    warn,
)

# The push client reports connection trouble through logging, and a service
# that reconnects silently is a service nobody can debug.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# The longest an activity can go unnoticed if no push arrives for it.
POLL_MINUTES = float(os.environ.get("WATCH_POLL_MINUTES") or 60)

# Daycare logs a nap, a photo and a diaper in one burst; waiting a moment turns that into one import instead of three.
SETTLE_SECONDS = float(os.environ.get("WATCH_SETTLE_SECONDS") or 30)


def import_once(procare, reason):
    """
    Import the current window, letting nothing stop the watch.
    """

    start, end = sync_range()

    log("")
    log(f"=== Importing {start}..{end} ({reason})")

    try:
        import_window(procare, start, end)
    except Exception as error:  # noqa: BLE001 - a bad import must not end the watch
        warn(f"import failed: {error!r}")


async def wait_for_work(woken, pushes):
    """
    Block until a push arrives or the poll interval runs out, and say
    which it was.
    """

    try:
        await asyncio.wait_for(woken.wait(), timeout=POLL_MINUTES * 60)
    except (asyncio.TimeoutError, TimeoutError):
        return f"nothing pushed for {POLL_MINUTES:g} minutes"

    # Let the rest of a burst land before importing.
    await asyncio.sleep(SETTLE_SECONDS)

    woken.clear()
    categories = ", ".join(sorted(set(pushes))) or "unknown"
    count = len(pushes)
    pushes.clear()

    return f"{count} push{'es' if count != 1 else ''}: {categories}"


async def watch():
    device = procare_fcm.credentials()

    woken = asyncio.Event()
    pushes = []

    def on_push(data, persistent_id, context):
        category = data.get("category") or "?"
        title = data.get("title") or ""
        message = data.get("message") or ""

        log(f"PUSH     {category} | {title} {message}".rstrip())

        pushes.append(category)
        woken.set()

    # Signing in is what subscribes this device, so it comes before we
    # listen - but only when the cached session isn't subscribed already,
    # since a restarted container should not open a session every time.
    # Later re-authentication carries the device with it either way.
    procare = ProcareClient(device=device)

    if not cached_session_subscribes(device):
        procare.sign_in()

    client = procare_fcm.client(device, on_push)
    await client.start()

    log(
        f"Watching for Procare pushes; importing at least every "
        f"{POLL_MINUTES:g} minutes."
    )

    reason = "startup"

    try:
        while True:
            await asyncio.to_thread(import_once, procare, reason)
            reason = await wait_for_work(woken, pushes)
    finally:
        await client.stop()


def main():
    try:
        asyncio.run(watch())
    except KeyboardInterrupt:
        log("Stopped.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
