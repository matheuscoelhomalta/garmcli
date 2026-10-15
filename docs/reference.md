# Usage reference

[garmcli](../README.md) is a read-only Garmin data CLI using the
[garminconnect](https://github.com/cyberjunky/python-garminconnect) Python library.
This reference describes garmcli 2.0.0, requiring Python
3.12+ and `garminconnect==0.3.18`.
garmcli owns authentication, its read-only request boundary, local history,
resumable sync, and JSON envelope. It is an independent community tool, not
affiliated with or endorsed by Garmin.

## Command structure

```text
garmcli [GLOBAL OPTIONS] auth login EMAIL [--domain garmin.com|garmin.cn]
garmcli [GLOBAL OPTIONS] auth status
garmcli [GLOBAL OPTIONS] auth logout
garmcli [GLOBAL OPTIONS] doctor
garmcli [GLOBAL OPTIONS] devices list
garmcli [GLOBAL OPTIONS] sync --from DATE --to DATE [--metrics LIST] [--refresh] [--delay SECONDS]
garmcli [GLOBAL OPTIONS] health METRIC --from DATE --to DATE [--offline] [--out PATH]
garmcli [GLOBAL OPTIONS] snapshot --date DATE [--metrics LIST] [--offline]
garmcli [GLOBAL OPTIONS] activities list [--limit NUMBER]
garmcli [GLOBAL OPTIONS] activities download ID --format FORMAT --out PATH
```

Global options must precede the command. Command-specific options follow it.
Use `garmcli --help` or append `--help` to a command or subcommand for standard
help text. Help remains text even when `--json` is selected.

| Global option | Default | Meaning |
| --- | --- | --- |
| `--json` | Off | Emit one versioned JSON envelope on stdout. |
| `--db PATH` | Platform-specific path below | Choose the SQLite history file. |
| `--account-id ID` | Automatic for cached reads | Select a cached account; during sync, require a match with the authenticated account. |
| `--timeout SECONDS` | `120` | Total timeout for each live data query or download, including retries and session refresh; must be a finite number greater than zero. |

The default history path is calculated as follows by the implementation; these
paths do not imply that every platform has been tested:

| Platform | Default history file |
| --- | --- |
| macOS | `~/Library/Application Support/garmcli/history.sqlite3` |
| Windows | `%LOCALAPPDATA%\garmcli\history.sqlite3`, falling back to `~/AppData/Local/garmcli/history.sqlite3` when `LOCALAPPDATA` is unset |
| Other platforms | `$XDG_DATA_HOME/garmcli/history.sqlite3`, falling back to `~/.local/share/garmcli/history.sqlite3` when `XDG_DATA_HOME` is unset |

An explicit `--db` is useful for keeping a dataset at a chosen location. History
commands create the database and parent directories when absent. Existing
unrelated files, unrelated SQLite databases, and unsupported history schemas are
refused rather than reset. `doctor` also opens or initializes the history file.

## Setup diagnostics and live queries

```bash
garmcli --json doctor
garmcli --json devices list
garmcli --json activities list --limit 10
```

`doctor` reports local backend-library availability and version under
`data.backend` (`available` and `version`), saved-session availability, account
identity when available, and whether the history file can be opened. It does not
contact Garmin or prove live endpoint reachability. Expired saved tokens may be
refreshed on the next live request; a saved session does not guarantee success.

`devices list` and `activities list` are live queries. Their envelope `data` is
the upstream JSON response. The activity limit defaults to `20` and accepts
integers from `1` through `1000`.

The positive finite `--timeout` defaults to 120 seconds per live data query or
download. An internal Python worker enforces this total limit across network
requests, refresh and retries. Interactive `auth login` is outside this worker
time limit. No separate runtime executable is required.

## Authentication

```bash
garmcli auth login your-email@example.com
garmcli --json auth status
garmcli --json doctor
garmcli auth logout
```

`auth login` starts a fresh login. It accepts an email and optional
`--domain garmin.com` (the default) or `--domain garmin.cn`. Passwords and MFA
codes are entered through hidden prompts, never command arguments or environment
variables. Interactive authentication needs a terminal; do not put secrets in
scripts or pipe them into the command.

Sessions are stored using `keyring` in macOS Keychain or Linux Secret Service.
If a secure store is unavailable, authentication fails rather than falling back
to plaintext storage. garmcli does not save passwords or expose a raw token
export command. `auth status` checks local state, not a Garmin endpoint.
`auth logout` removes only garmcli's local session; it does not delete Garmin
records or local SQLite history.

When upgrading from 1.0.0, sign in again with `auth login`. Existing SQLite
history needs no migration or reset. Using the same normalized email and domain
preserves account IDs. The old `--gccli` global option is removed. Installing
garmcli supplies its required Python libraries automatically; see the
[installation instructions](../README.md#install).

## Sync dated health history

```bash
garmcli --json sync --from 2026-01-01 --to 2026-01-03
garmcli --json sync --from 2026-01-01 --to 2026-01-03 --metrics summary,sleep,hrv
```

Dates must be valid calendar dates in strict `YYYY-MM-DD` format. The start date
must be on or before the end date. Both endpoints are included: January 1–3
requests three dates. Dates represent the Garmin calendar date requested, which
can differ from timestamps embedded in a response.

By default, sync requests all 15 metric families in the following order:

| Metric name | Data family |
| --- | --- |
| `summary` | Daily summary |
| `steps` | Steps |
| `heart-rate` | Heart rate |
| `floors` | Floors |
| `sleep` | Sleep |
| `stress` | Stress |
| `respiration` | Respiration |
| `spo2` | Pulse oximetry |
| `hrv` | Heart rate variability |
| `body-battery` | Body Battery |
| `intensity-minutes` | Intensity minutes |
| `training-readiness` | Training readiness |
| `training-status` | Training status |
| `fitness-age` | Fitness age |
| `max-metrics` | Max metrics |

`--metrics` accepts a comma-separated selection of these exact names, without
spaces. Repeated names are deduplicated while preserving their first occurrence.
Sync visits dates in ascending order and metrics in the selected order.

| Sync option | Default | Behavior |
| --- | --- | --- |
| `--from DATE`, `--to DATE` | Required | Inclusive date range. |
| `--metrics LIST` | All 15 families | Restrict the requested date/metric grid. |
| `--refresh` | Off | Fetch even records already marked `available` or `empty`. |
| `--delay SECONDS` | `0.5` | Wait between fetch attempts; accepts finite nonnegative numbers, including `0`. |

Each completed response is committed atomically to account-scoped SQLite
history. Repeat the same range and metrics with the same database and account to
resume an interrupted or incomplete sync:

- `available` and `empty` records are skipped unless `--refresh` is supplied.
- Stored `error` records are retried, and `missing` records are fetched.
- A retry or refresh replaces the stored response and increments its `attempts`.

The native backend refreshes a session after HTTP 401 and retries rate limits
(HTTP 429) up to three times, with each wait capped at 30 seconds, and server
errors (HTTP 5xx) up to twice, within the operation timeout. Ordinary endpoint
errors are stored explicitly and independent requests continue. Persistent
authentication failures and rate limits stop the run. A failed request is never
stored as zero or as an empty response.

Ctrl+C preserves already-committed responses. An interrupted sync reports
`data.interrupted: true`, `complete: false`, and exits `130`; repeat the same sync
command to resume. Progress is written to stderr as each fetch finishes.

Sync returns the account ID, range, selected metrics, and `counts`:

| Count | Meaning |
| --- | --- |
| `requested` | Number of date/metric pairs in the requested grid. |
| `fetched` | Attempts completed during this run, including failures. |
| `skipped` | Completed records reused during this run. |
| `available`, `empty`, `errors`, `missing` | Final record-status counts across the requested grid. |

## Read and export cached history

`health` and `snapshot` always read local history, even without `--offline`.
They never fetch implicitly and require neither Garmin authentication nor the
live backend libraries. They do not import the SDK or keyring, access the
secure session store, or make network requests.
Run `sync` first when fresh data is needed.

```bash
garmcli --json health sleep --from 2026-01-01 --to 2026-01-03 --offline
garmcli --json health heart-rate --from 2026-01-01 --to 2026-01-03 --out ./heart-rate.json
garmcli --json snapshot --date 2026-01-03 --offline
garmcli --json snapshot --date 2026-01-03 --metrics summary,sleep
```

`health` takes exactly one metric from the table above and an inclusive date
range. `--out PATH` writes the same full JSON envelope returned by the
command, regardless of whether stdout uses `--json`. The output directory must
already exist; an existing destination is refused. An incomplete query can still
write an export: inspect `ok`, `complete`, record statuses, and the exit code
before using it for analysis.

`snapshot` takes one date and optionally `--metrics`. Its default selection is
`summary,sleep,hrv,training-readiness`. It returns the same record structure as
`health`; it does not have an `--out` option.

### Account selection

When the selected database contains exactly one account, cached reads select it
automatically. With multiple cached accounts, supply `--account-id`. With no
cached accounts, sync the intended account first or explicitly select an ID.
An explicit ID with no matching records returns a `missing` grid and an
incomplete result.

```bash
garmcli --json --db ./history.sqlite3 --account-id SYNTHETIC_ACCOUNT_ID health steps --from 2026-01-01 --to 2026-01-03
```

Use an actual `account_id` returned by `doctor`, `sync`, or a prior cached query
in place of the synthetic example. Account IDs are SHA-256 hashes of
`normalized-domain:normalized-email`: both components are trimmed and
case-folded. They distinguish Garmin service domains and accounts; they are not
authentication credentials.

Sync resolves the authenticated garmcli account and pins its health requests to
that identity. Passing a different `--account-id` fails with `account_mismatch`;
it cannot redirect live data into another account's history. The option does not
switch accounts for `doctor`, device queries, or activity queries. Manage the
active live account through `garmcli auth login`.

## Download activity files

```bash
garmcli --json activities download 123456789 --format fit --out ./activity.fit
```

The activity ID must be a positive integer. Accepted formats are `fit`, `gpx`,
`tcx`, `kml`, and `csv`; format availability for a particular activity depends on
the upstream service. `--format` and `--out` are required. The destination's
parent directory must exist, and an existing destination is refused.

The download is a live request, separate from dated health history. On success,
the file is published only after the backend produces a nonempty regular file. The
JSON envelope contains metadata (`path`, `bytes`, `activity_id`, `format`), not
the file contents. garmcli does not decode or validate the activity format.

## JSON and process contract

With `--json`, stdout contains one JSON envelope, including command failures.
Sync progress goes to stderr. Without `--json`, stdout contains a short result
line and formatted JSON data, while error messages go to stderr. Help is always
standard text.

```json
{
  "schema_version": "1",
  "command": "health",
  "ok": true,
  "data": {
    "account_id": "SYNTHETIC_ACCOUNT_ID",
    "from": "2026-01-01",
    "to": "2026-01-01",
    "metrics": ["sleep"],
    "records": [
      {
        "date": "2026-01-01",
        "metric": "sleep",
        "status": "empty",
        "data": {},
        "error": null,
        "fetched_at": "2026-01-02T12:00:00+00:00",
        "attempts": 1
      }
    ]
  },
  "errors": [],
  "complete": true
}
```

The example is synthetic. The envelope always contains `schema_version`,
`command`, `ok`, `data`, and `errors`. `command` identifies the top-level command
(`activities` for either activity subcommand). `data` can be `null` on failure.
Errors have `code` and `message`, optionally an HTTP `status`; history errors also
identify their `date` and `metric`. External failure messages are sanitized;
arbitrary backend responses, credentials and session tokens are not echoed as
error messages.

Grid-operation results (`sync`, `health`, `snapshot`) also include a
`complete` boolean. Early validation or setup failures can omit it. `ok` is true
only when there are no errors and, when supplied, `complete` is true.
`complete: true` means every requested date/metric has a successful response,
including confirmed empty responses. It does **not** mean every date contains
measurements. For sync it also requires that the run was not interrupted. Check
counts and statuses before analysis.

Every history record has `date`, `metric`, `status`, `data`, `error`, `fetched_at`,
and `attempts`. `fetched_at` is the UTC retrieval timestamp, not the measurement
time. Missing records have `fetched_at: null` and `attempts: 0`; stored attempts
count completed saves, including saved failures.

| Status | Meaning |
| --- | --- |
| `available` | Successful nonempty response; individual fields may still be missing. |
| `empty` | Successful top-level `null`, `[]`, or `{}`. |
| `error` | Failed request with explicit error details. |
| `missing` | No stored result for that account, metric, and date. |

The outer contract is controlled by garmcli. History record `data` preserves
Garmin's parsed JSON payload without normalizing its fields; Garmin's inner
schema can change independently. Device and activity-list payloads likewise
retain the upstream schema.

| Exit code | Meaning |
| --- | --- |
| `0` | Success, including confirmed empty results. |
| `1` | Other failure or incomplete query/export. |
| `2` | Invalid input. |
| `3` | Authentication required for the attempted live operation. |
| `5` | Live operation stopped by a rate limit. |
| `130` | Interrupted operation, including interrupted sync. |

Cached queries containing previously stored authentication or rate-limit errors
are incomplete queries and exit `1`; they do not attempt live authentication or
network access. An activity-download destination that already exists is reported
as `invalid_input` (exit `2`); a health-export destination that exists is
`destination_exists` (exit `1`).

## Interpreting Garmin data

- Numbers equal to zero remain data. A 404 or an empty response does not establish
  that a watch lacks a feature.
- Raw streams can contain nulls and negative sentinel values. Use their
  descriptors and metric-specific semantics before calculating statistics.
  Preserve units, timestamps, and device attribution.
- The requested date is not always the measurement date. Some endpoints return
  latest-style values or nested historical timestamps. Check embedded dates and
  timestamps.
- Sleep's calendar date generally identifies the night ending on that day.
  Skin temperature in sleep exports is deviation from a baseline, not absolute
  body temperature.
- Dedicated Body Battery data may have fewer points than the Body Battery series
  included in the stress response.
- Resting heart rate is available in `summary`, `heart-rate`, and `sleep`.
  Standalone resting-HR and race-prediction commands are outside this command surface.

## Troubleshooting

| Symptom or error | Supported next action |
| --- | --- |
| Backend library unavailable | Repair the garmcli installation so its declared Python dependencies are installed, then run `doctor` again. |
| Secure session store unavailable | Make macOS Keychain or Linux Secret Service available to this user session, then rerun the auth command. There is no plaintext fallback. |
| Live authentication failure (exit `3`) | Check `garmcli --json auth status` and sign in with `garmcli auth login your-email@example.com` when needed, then rerun the command. Expired saved credentials alone do not prove login is necessary. |
| Rate limit (exit `5`) | Stop live requests and retry later. Resume the same sync to reuse completed records; `--delay` controls spacing between subsequent fetch attempts. garmcli does not specify a Garmin cooldown duration. |
| `account_required` with no cached account | Sync the intended range first, or select a known cached account ID and the correct `--db`. Supplying an ID alone does not fetch data. |
| `account_required` with multiple cached accounts | Pass the intended `--account-id` before the command. |
| `account_mismatch` | Use the ID matching the authenticated account, or sign in to the intended live account with `garmcli auth login` before syncing. |
| `incomplete_history`, stored errors, or `complete: false` | Inspect statuses and errors; sync the same range and metrics to fetch missing records and retry failures, then rerun the cached query/export. |
| Confirmed `empty` result that needs another fetch | Use `sync --refresh` with the required range and metrics; ordinary resumption skips empty records. |
| `destination_exists`, or activity `invalid_input` saying the destination exists | Choose a new output path; exports do not overwrite existing files. |
| `history_unavailable`, `local_error`, or `filesystem_error` | Check the database/output path, parent directory, permissions, and history schema. Use `--db` to select a valid garmcli database or a new unused path; unrelated or unsupported databases are refused. |
| `timeout` | Check setup and connectivity; increase the positive finite global `--timeout` if a longer total live-operation limit is appropriate. |
| `invalid_input` for dates, metrics, limits, or numeric options | Match the command syntax and bounds above; global options precede commands. |

## Read-only scope and personal data

garmcli exposes a fixed read-only GET allowlist for data requests. Login and
session refresh perform the authentication requests required by Garmin. It does not create,
schedule, upload, edit, or delete Garmin records. Local sync and exports do write
files: history is account-scoped, and each response is committed atomically.
History and exports can contain personal health data and GPS locations, even
though authentication tokens are not exported. Keep personal exports, databases,
passwords, and tokens out of bug reports and pull requests; use synthetic data.

For implementation details, see [the parser and workflows](../src/garmcli/cli.py),
[upstream adapter](../src/garmcli/backend.py),
[record contracts](../src/garmcli/contracts.py), and
[history store](../src/garmcli/store.py).
