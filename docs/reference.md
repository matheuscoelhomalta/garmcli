# Usage reference

[garmcli](../README.md) is a read-only Garmin data CLI backed by
[gccli](https://github.com/bpauli/gccli). This reference describes garmcli 1.0.0,
which requires Python 3.11+ and was tested with gccli 1.13.0. Live Garmin access
and authentication belong to gccli; local history, resumable sync, and the JSON
envelope belong to garmcli. garmcli is an independent community tool, not
affiliated with or endorsed by Garmin.

## Command structure

```text
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
| `--gccli PATH` | `gccli` | Choose the upstream executable, otherwise resolved through `PATH`. |
| `--account-id ID` | Automatic for cached reads | Select a cached account; during sync, require a match with the authenticated account. |
| `--timeout SECONDS` | `120` | Timeout for each gccli subprocess; must be a finite number greater than zero. |

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

`doctor` reports the gccli version, saved-credential availability, account identity
when available, and whether the history file can be opened. It checks local setup
and saved credentials, not live endpoint reachability. Expired saved tokens can
still be refreshed by gccli on the next live request; the presence of saved
credentials does not guarantee a successful Garmin request.

`devices list` and `activities list` are live queries. Their envelope `data` is
the upstream JSON response. The activity limit defaults to `20` and accepts
integers from `1` through `1000`.

Live queries require a working gccli installation and Garmin Connect session.
If authentication is missing, sign in using gccli, then rerun `doctor`:

```bash
gccli auth login your-email@example.com
garmcli --json doctor
```

On macOS, use the gccli Keychain backend. garmcli does not store passwords or
export authentication tokens.

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

| Metric name | gccli command used before the date and `--json` |
| --- | --- |
| `summary` | `health summary` |
| `steps` | `health steps view` |
| `heart-rate` | `health hr` |
| `floors` | `health floors` |
| `sleep` | `health sleep` |
| `stress` | `health stress view` |
| `respiration` | `health respiration` |
| `spo2` | `health spo2` |
| `hrv` | `health hrv` |
| `body-battery` | `health body-battery view` |
| `intensity-minutes` | `health intensity-minutes view` |
| `training-readiness` | `health training-readiness` |
| `training-status` | `health training-status` |
| `fitness-age` | `health fitness-age` |
| `max-metrics` | `health max-metrics` |

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

gccli owns HTTP retries and token refresh; garmcli does not add a retry loop around
each failed request. Ordinary endpoint errors are stored explicitly and
independent requests continue. Authentication failures and rate limits stop the
run. A failed request is never stored as zero or as an empty response.

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
They never fetch implicitly and require neither gccli nor Garmin authentication.
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

Sync resolves the authenticated gccli account and pins its health requests to
that identity. Passing a different `--account-id` fails with `account_mismatch`;
it cannot redirect live data into another account's history. The option does not
switch gccli accounts for `doctor`, device queries, or activity queries. Manage
the active live account through gccli.

## Download activity files

```bash
garmcli --json activities download 123456789 --format fit --out ./activity.fit
```

The activity ID must be a positive integer. Accepted formats are `fit`, `gpx`,
`tcx`, `kml`, and `csv`; format availability for a particular activity depends on
the upstream service. `--format` and `--out` are required. The destination's
parent directory must exist, and an existing destination is refused.

The download is a live request, separate from dated health history. On success,
the file is published only after gccli produces a nonempty regular file. The
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
arbitrary gccli stdout/stderr is not echoed as an error message.

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
  Standalone resting-HR and race-prediction commands are outside the v1 surface.

## Troubleshooting

| Symptom or error | Supported next action |
| --- | --- |
| `executable_missing` | Install gccli or place it on `PATH`; alternatively pass `--gccli PATH` before the command. Run `doctor` again. |
| Live authentication failure (exit `3`) | Check the saved session with gccli and sign in with `gccli auth login your-email@example.com` when needed, then rerun the command. Expired saved credentials alone do not prove login is necessary. |
| Rate limit (exit `5`) | Stop live requests and retry later. Resume the same sync to reuse completed records; `--delay` controls spacing between subsequent fetch attempts. garmcli does not specify a Garmin cooldown duration. |
| `account_required` with no cached account | Sync the intended range first, or select a known cached account ID and the correct `--db`. Supplying an ID alone does not fetch data. |
| `account_required` with multiple cached accounts | Pass the intended `--account-id` before the command. |
| `account_mismatch` | Use the ID matching the authenticated gccli account, or switch the live account through gccli before syncing. |
| `incomplete_history`, stored errors, or `complete: false` | Inspect statuses and errors; sync the same range and metrics to fetch missing records and retry failures, then rerun the cached query/export. |
| Confirmed `empty` result that needs another fetch | Use `sync --refresh` with the required range and metrics; ordinary resumption skips empty records. |
| `destination_exists`, or activity `invalid_input` saying the destination exists | Choose a new output path; exports do not overwrite existing files. |
| `history_unavailable`, `local_error`, or `filesystem_error` | Check the database/output path, parent directory, permissions, and history schema. Use `--db` to select a valid garmcli database or a new unused path; unrelated or unsupported databases are refused. |
| `timeout` | Check gccli setup and connectivity; increase the positive finite global `--timeout` if a longer subprocess wait is appropriate. |
| `invalid_input` for dates, metrics, limits, or numeric options | Match the command syntax and bounds above; global options precede commands. |

## Read-only scope and personal data

garmcli exposes a fixed read-only upstream command allowlist. It does not create,
schedule, upload, edit, or delete Garmin records. Local sync and exports do write
files: history is account-scoped, and each response is committed atomically.
History and exports can contain personal health data and GPS locations, even
though authentication tokens are not exported. Keep personal exports, databases,
passwords, and tokens out of bug reports and pull requests; use synthetic data.

For implementation details, see [the parser and workflows](../src/garmcli/cli.py),
[upstream adapter](../src/garmcli/backend.py),
[record contracts](../src/garmcli/contracts.py), and
[history store](../src/garmcli/store.py).
