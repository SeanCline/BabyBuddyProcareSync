# Procare → Baby Buddy

Imports a day of daycare activities from [Procare](https://www.procaresoftware.com/)
into [Baby Buddy](https://github.com/babybuddy/babybuddy), so what the daycare
logs during the day ends up alongside what you log at home.

Re-running is a no-op: every record it creates carries its Procare UUID, and
each run reads those back before writing anything.

## What it imports

| Procare activity | Baby Buddy record | Notes |
| --- | --- | --- |
| `bathroom_activity` | Diaper change | `wet` / `solid` from the sub-type; diaper cream noted |
| `bottle_activity` | Feeding | Bottle, amount in oz, type from Procare or `$BABYBUDDY_BOTTLE_TYPE` |
| `nap_activity` (×2) | Sleep | A start and an end record merged into one nap — see below |
| `photo_activity` | Note | The photo is downloaded and attached to the note |
| `video_activity` | Note | The still frame is attached; the video is saved to `videos/` — see below |
| `sign_in_activity` | Note | "Signed in by …", with the room |
| `sign_out_activity` | Note | "Signed out by …", with the room |

Anything else is skipped with a warning naming the activity type.

Every record is tagged (`daycare` by default) and its note begins with
`[Procare ID: <uuid>]`, which is what makes the import idempotent.

## How naps are merged

Procare logs a nap as **two separate activities**: one carrying
`data.start_time`, and later another carrying `data.end_time`. They share no
id, batch or index — the only thing linking them is the clock. So within a
single day, an end closes the most recent still-open start.

Anything ambiguous is reported rather than guessed at:

| Situation | Behaviour |
| --- | --- |
| Start with no end yet | Held back; imported on a later run once daycare logs the wake-up |
| End with no start that day | Warned and skipped |
| Two starts in a row | Warned; the later start is paired (the conservative, shorter nap) |
| Pair longer than `$MAX_NAP_HOURS` | Warned and skipped as mis-paired |
| End at or before its start | Warned and skipped |

The merged sleep record carries **both** Procare UUIDs, so it is recognised
from either half on the next run.

Pairing is scoped to one calendar day, which means a nap crossing midnight is
never paired — irrelevant for daycare hours, and it keeps a start from pairing
with an unrelated day's end.

## Videos

A Baby Buddy note holds one image and nothing else, so a video becomes a note
with its still frame attached, exactly like a photo. The video itself is
downloaded to `videos/` beside the script — Procare's links to them are signed
and expire within a few months, so this file ends up being the only lasting
copy. The directory is created on first use and can be moved with
`$PROCARE_VIDEO_DIR`.

Files are named after the Procare activity, and a video already on disk is
never fetched again:

    videos/procare-0cc399e2-bacb-4e35-9741-916d958e92e1.mp4

The note names the file, so a note and its video can always be matched up:

    [Procare ID: 0cc399e2-bacb-4e35-9741-916d958e92e1]
    Room: Bunnies Room
    Leg work out! AP
    Video: procare-0cc399e2-bacb-4e35-9741-916d958e92e1.mp4

In the container `videos/` lands in the bind-mounted script directory, so the
files outlive the container.

If a download fails the note records the Procare link instead, and the next run
tries the video again: archiving happens before the duplicate check, so an
existing note is no reason to leave its video missing. Since each run only asks
Procare for one day, those retries are the remaining runs of that day — after
that, re-run the day by hand (`daycare_babybuddy_sync.py 2026-09-15`) while the
link still works. A note written during the failure keeps the link in its text
even once the file is saved; the file is named after the Procare ID in that
same note, so the two still match up.

## Requirements

Python 3.9+ and [`requests`](https://pypi.org/project/requests/) for the
import. Watching for push notifications also needs
[`firebase-messaging`](https://pypi.org/project/firebase-messaging/):

```sh
pip install requests firebase-messaging
```

| File | What it is |
| --- | --- |
| `daycare_babybuddy_sync.py` | The import. Runs standalone, needs only `requests` |
| `procare_fcm.py` | Registers with Firebase as an Android device and receives the pushes |
| `scripts/procare_watch.py` | Long-running service: imports on a push, and on a timer regardless |
| `scripts/push_probe.py` | Diagnostic: logs incoming notifications and flags unexpected ones |

## Configuration

Everything is read from the environment.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `BABYBUDDY_URL` | **yes** | — | Base URL, e.g. `http://baby.example.com:8000/` |
| `BABYBUDDY_TOKEN` | **yes** | — | API key from Baby Buddy → User → Settings |
| `BABYBUDDY_CHILD_ID` | **yes** | — | Numeric child id |
| `SYNC_START_DATE` | no | — | First date to import; `YYYY-MM-DD`, `today` or `yesterday` |
| `SYNC_END_DATE` | no | `today` | Last date to import |
| `SYNC_DAYS_BACK` | no | `1` | Days of history before the end date, when no start is given |
| `BABYBUDDY_TAG` | no | `daycare` | Tag applied to every imported record |
| `BABYBUDDY_BOTTLE_TYPE` | no | `breast milk` | Used when Procare doesn't say what was in the bottle |
| `MAX_NAP_HOURS` | no | `6` | Above this, a merged nap is treated as mis-paired |
| `WATCH_POLL_MINUTES` | no | `60` | `procare_watch.py`: longest gap between imports when nothing pushes |
| `WATCH_SETTLE_SECONDS` | no | `30` | How long to let a burst of pushes finish before importing |
| `PROCARE_EMAIL` | no | — | Procare login; also prompted for interactively |
| `PROCARE_PASSWORD` | no | — | As above |
| `PROCARE_TOKEN` | no | — | Use an existing session token instead of signing in |
| `PROCARE_TOKEN_CACHE` | no | `.procare-token.json` beside the script | Where the session token is cached |
| `PROCARE_VIDEO_DIR` | no | `videos/` beside the script | Where videos are saved; created if missing |
| `PROCARE_FCM_API_KEY` | for push | — | Procare's Firebase key, recovered from their APK (below) |
| `PROCARE_FCM_CACHE` | no | `.procare-fcm.json` beside the script | Where the push device registration is cached |
| `PROCARE_KID_ID` | no | auto | Discovered automatically when the account has one child |
| `PROCARE_API` | no | Procare's mobile API | Override the API base URL |
| `PROCARE_AUTH_API` | no | Procare's auth host | Override where sign-in happens |

### Signing in

The first run signs in and caches the session token, so later runs need no
credentials. Credentials come from `$PROCARE_EMAIL` / `$PROCARE_PASSWORD`, or
an interactive prompt when neither is set.

Sign-in posts to Procare's auth host, which is separate from the API host and
hands back a token the API accepts. It is the same call the phone app makes,
including the `fcm_token` that subscribes a device to push notifications when
`procare_watch.py` supplies one. Procare also serves these activities under an
`/api/web/` namespace, but that one rejects tokens from this sign-in.

If the cached token expires, the next run signs in again and retries. Where
that isn't possible — no credentials and no terminal, as in a container — the
run ends telling you to set them.

The cache file holds a live session token. It is written `0600` and should
stay out of version control.

## Usage

```sh
daycare_babybuddy_sync.py                      # today and yesterday
daycare_babybuddy_sync.py 2026-08-17           # one specific day
daycare_babybuddy_sync.py --start 2026-08-10 --end 2026-08-17
daycare_babybuddy_sync.py --days-back 7        # the last week, up to today
daycare_babybuddy_sync.py --end yesterday --days-back 0
daycare_babybuddy_sync.py --dry-run            # show what would be created
daycare_babybuddy_sync.py --from-file day.json # replay a saved API response
daycare_babybuddy_sync.py --login              # sign in again, replacing the cache
```

### The date range

Every run imports a window of days, and re-importing is free — records already
in Baby Buddy are recognised and skipped — so the window can be as wide as is
useful.

Either end can be pinned with `--start` and `--end`, which take `YYYY-MM-DD`,
`today` or `yesterday`. Whichever end is left open is filled in: `--end`
defaults to today, and `--start` to `--days-back` days before the end. So
`--days-back 7` means the last week, `--days-back 0` means the end date alone,
and a bare date is shorthand for `--start DATE --end DATE`.

The default is `--days-back 1`: today and yesterday. Yesterday is worth
re-reading because daycare keeps writing after the last run of the evening, and
a nap that ends after midnight is logged against the day it started.

The same window can be set in the environment with `$SYNC_START_DATE`,
`$SYNC_END_DATE` and `$SYNC_DAYS_BACK`, which is how the scheduled container
does it. Command-line flags win over those. Pinning a start date and giving a
day count is contradictory: the start date wins and the run says so.

`--from-file` reads a saved Procare response (the JSON body of
`/api/web/parent/daily_activities/`) instead of calling the API, which makes it
easy to work on the conversion offline. Combined with `--dry-run` it touches
nothing at all. A saved file is read whole unless the command line asks for
particular dates, so a fixture from any day stays usable.

A run prints one line per planned record:

```
IMPORT   nap_activity         2026-08-17T13:32:44.801-04:00 -> sleep
         Baby Buddy ID: 403
SKIP dup bathroom_activity    2026-08-17T14:08:49.138-04:00 -> changes
WAIT     nap_activity         2026-08-17T15:10:02.334-04:00 still in progress; will import once daycare logs the wake-up.

Imported:   1
Duplicate:  1
```

## Running it as a service

`scripts/procare_watch.py` imports when daycare logs something, instead of asking
every few minutes whether they have.

Procare's phone app receives push notifications through Firebase Cloud
Messaging, and `procare_fcm.py` registers with Firebase the way that app does:
a device check-in, a Firebase installation, then a registration for Procare's
sender id. The resulting token is handed to Procare at sign-in — its login
takes an `fcm_token`, which is exactly how the app subscribes a phone — and
from then on this device gets the same notifications.

A push carries no useful detail, only that something happened, so it is used
as a hint: wait `$WATCH_SETTLE_SECONDS` for the rest of the burst to land, then
run the ordinary import over the configured window.

**Nothing depends on a push arriving.** An import runs at least every
`$WATCH_POLL_MINUTES` (default 60) whatever happens, so a dropped socket, a
revoked token or an activity Procare simply does not notify about costs a
delay, never a missing record. An import that fails is logged and the watch
carries on.

The registration is cached in `.procare-fcm.json` beside the script and reused,
since each registration is a device that shows up on Procare's side.

### Procare's Firebase key

Registering needs Procare's own Firebase API key. It is not in this repository:
it belongs to Procare rather than to us, and a public copy of someone else's
credential attracts scanners even when — as here — it is a project identifier
that ships inside every install of their app rather than anything secret.

Recover it from the Procare APK and set `$PROCARE_FCM_API_KEY`:

```sh
unzip -p Procare.apk resources.arsc | grep -ao 'AIza[A-Za-z0-9_-]\{35\}'
```

The other identifiers in `procare_fcm.py` — project, app id, sender id, package,
signing certificate — come from the same APK and are checked in, since nothing
treats them as credentials. If Procare rotates the key, registration fails with
a message naming this variable; existing registrations keep working.

### Deploying

`procare_sync.yaml` is a Compose stack (built for Portainer) holding one
container that stays up. To deploy:

1. Copy the repository to the Docker host, e.g. `/opt/procare`, keeping
   `scripts/` beside the two modules — the scripts import them from there.
2. Add the stack in Portainer, setting at least `PROCARE_DIR`, `PROCARE_EMAIL`,
   `PROCARE_PASSWORD`, `BABYBUDDY_URL`, `BABYBUDDY_TOKEN` and
   `BABYBUDDY_CHILD_ID` in the environment variables box.

```sh
docker logs -f procare-sync
```

The container idles on an open socket to Firebase rather than sitting stopped
between runs, which costs a resident Python process — the price of hearing
about a nap when it happens rather than up to ten minutes later.

### What actually pushes

`scripts/push_probe.py` listens and writes every notification to
`push-log.jsonl`,
flagging anything whose shape differs from what the app's own code expects:

```sh
python scripts/push_probe.py --hours 9
```

Run it for a day to learn which activities Procare really notifies about. It
signs in the same way, so don't run it alongside `procare_watch.py` — both
would connect to Firebase as the same device.

## License

MIT — see [LICENSE.md](LICENSE.md).
