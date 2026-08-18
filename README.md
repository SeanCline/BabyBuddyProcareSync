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

## Requirements

Python 3.9+ and [`requests`](https://pypi.org/project/requests/):

```sh
pip install requests
```

## Configuration

Everything is read from the environment.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `BABYBUDDY_URL` | **yes** | — | Base URL, e.g. `http://baby.example.com:8000/` |
| `BABYBUDDY_TOKEN` | **yes** | — | API key from Baby Buddy → User → Settings |
| `BABYBUDDY_CHILD_ID` | **yes** | — | Numeric child id |
| `BABYBUDDY_TAG` | no | `daycare` | Tag applied to every imported record |
| `BABYBUDDY_BOTTLE_TYPE` | no | `breast milk` | Used when Procare doesn't say what was in the bottle |
| `MAX_NAP_HOURS` | no | `6` | Above this, a merged nap is treated as mis-paired |
| `PROCARE_EMAIL` | no | — | Procare login; also prompted for interactively |
| `PROCARE_PASSWORD` | no | — | As above |
| `PROCARE_TOKEN` | no | — | Use an existing session token instead of signing in |
| `PROCARE_TOKEN_CACHE` | no | `.procare-token.json` beside the script | Where the session token is cached |
| `PROCARE_KID_ID` | no | auto | Discovered automatically when the account has one child |
| `PROCARE_API` | no | Procare's API | Override the API base URL |

### Signing in

The first run signs in and caches the session token, so later runs need no
credentials. Credentials come from `$PROCARE_EMAIL` / `$PROCARE_PASSWORD`, or
an interactive prompt when neither is set.

If the cached token expires, the next run signs in again and retries. Where
that isn't possible — no credentials and no terminal, as in a container — the
run ends telling you to set them.

The cache file holds a live session token. It is written `0600` and should
stay out of version control.

## Usage

```sh
daycare_babybuddy_sync.py                      # import today
daycare_babybuddy_sync.py 2026-08-17           # import a specific day
daycare_babybuddy_sync.py --dry-run            # show what would be created
daycare_babybuddy_sync.py --from-file day.json # replay a saved API response
daycare_babybuddy_sync.py --login              # sign in again, replacing the cache
```

`--from-file` reads a saved Procare response (the JSON body of
`/api/web/parent/daily_activities/`) instead of calling the API, which makes it
easy to work on the conversion offline. Combined with `--dry-run` it touches
nothing at all.

A run prints one line per planned record:

```
IMPORT   nap_activity         2026-08-17T13:32:44.801-04:00 -> sleep
         Baby Buddy ID: 403
SKIP dup bathroom_activity    2026-08-17T14:08:49.138-04:00 -> changes
WAIT     nap_activity         2026-08-17T15:10:02.334-04:00 still in progress; will import once daycare logs the wake-up.

Imported:   1
Duplicate:  1
```

## Running it on a schedule

`procare_sync.yaml` is a Compose stack (built for Portainer) that runs the
import every 10 minutes on weekdays between 08:00 and 17:00.

The importer is deliberately **not** a long-running service. `procare-sync`
sits stopped between runs, holding no memory; a small
[ofelia](https://github.com/mcuadros/ofelia) container is the only resident
process, and it starts the existing importer container on a cron. Each run
does its work, prints its summary and exits.

To deploy:

1. Copy `daycare_babybuddy_sync.py` to the Docker host, e.g. `/opt/procare`.
2. Add the stack in Portainer, setting at least `PROCARE_DIR`, `BABYBUDDY_URL`,
   `BABYBUDDY_TOKEN` and `BABYBUDDY_CHILD_ID` in the environment variables box.

Output from every run appears in the scheduler's logs, because ofelia captures
the container's output:

```sh
docker logs -f procare-scheduler
```

The schedule lives in the ofelia config generated inside the stack file, as
6-field cron (`second minute hour day month weekday`):

```ini
schedule = 0 */10 8-16 * * 1-5   # every 10 min, 08:00–16:50, Mon–Fri
schedule = 0 0 17 * * 1-5        # a final run at 17:00 to catch sign-out
```

The scheduler needs `/var/run/docker.sock` in order to start the importer
container. That is full control of the Docker daemon, which is the trade for
not keeping a Python process resident between runs.

## License

MIT — see [LICENSE.md](LICENSE.md).
