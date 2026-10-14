---
name: garmcli
description: "Reads and exports personal Garmin Connect health and activity data with garmcli. Use for Garmin sleep, HRV, heart rate, stress, Body Battery and training-data queries; dated exports; resumable synchronization; and offline history analysis. Do not use for creating, scheduling, uploading, editing or deleting Garmin records, or for developing the CLI."
---

# Garmcli

Use the installed `garmcli` command from any working directory. It wraps gccli for read-only Garmin access and saves responses in account-scoped SQLite history. It does not modify records on Garmin.

## Start with the requested data

1. Verify `command -v garmcli`. Use `garmcli --help` and subcommand help when the command surface is unfamiliar. If the executable is missing, report the missing installation; do not install unrelated tools or silently switch backends.
2. Resolve the requested metric, date range and output destination from the user's request and available context. Dates are inclusive, strict `YYYY-MM-DD`; replace example dates below with the requested range. Resolve relative periods in the user's timezone. Prefer complete local days unless the user requests today's partial data. Clarify only essential unresolved scope; preserve settled choices.
3. Prefer stored history for historical queries. `health` and `snapshot` always read SQLite, even without `--offline`, and need neither Garmin authentication nor gccli. Check completeness and `fetched_at`; disclose stale or partial data when it affects the answer.
4. When requested data is missing or the user requests current data, sync the smallest required metric/date set, then query history. Before the first live operation, use `garmcli --json doctor` to check binary, saved credentials and database setup. A successful doctor does not verify live endpoint reachability.

Global flags precede the command: `--json`, `--db PATH`, `--gccli PATH`, `--account-id ID`, `--timeout SECONDS`. Use `--json` for parsed output; help remains plain text. On macOS the default history is under `~/Library/Application Support/garmcli/history.sqlite3`. Preserve an explicitly selected database throughout the task.

## Resumable sync

```bash
garmcli --json sync --from 2026-09-14 --to 2026-10-13 \
  --metrics summary,sleep,hrv
```

Each completed request commits separately. Resume an interrupted or failed sync by repeating the same database, date range and metrics. Successful `available` and `empty` results are skipped; `error` results are retried; `missing` results are fetched. Do not add `--refresh` merely to resume. For explicitly requested fresh data, use `--refresh` on the relevant dates; confirmed empty responses can change after later device synchronization.

Read `data.counts`: `requested`, `fetched`, `skipped`, `available`, `empty`, `errors`, `missing`. Also check `ok`, `complete`, `errors` and `data.interrupted`. Ordinary endpoint errors are recorded while independent requests continue; authentication and rate-limit failures stop the run. gccli owns HTTP retries and token refresh; preserve the default request delay unless there is a reason to change it. Do not repeatedly retry authentication failures or rate limits without addressing their cause.

`complete: true` means every requested metric/date received a successful response, including empty results. It does not establish that measurements exist on every day. After SIGINT, completed records remain saved and exit 130 indicates interruption.

## Offline queries and exports

```bash
garmcli --json health sleep --from 2026-09-14 --to 2026-10-13 --offline

garmcli --json health heart-rate --from 2026-09-14 --to 2026-10-13 \
  --offline --out ./heart-rate.json

garmcli --json snapshot --date 2026-10-13 --offline
```

For a selected backup, put `--db ./history.sqlite3` before the command. Offline queries select the sole stored account automatically; if several are present, resolve the intended identity and use `--account-id`. A live sync only accepts the authenticated account's identity. IDs are hashes, not login credentials.

`health <metric> --help` lists supported metrics. `snapshot` defaults to summary, sleep, HRV and training readiness; select a different set with `--metrics`. Query only needed metrics and dates because raw responses can be large.

Health results live in `data.records`, ordered by date. Every record has `date`, `metric`, `status`, `data`, `error`, `fetched_at`, and `attempts`. `record.data` is the unmodified Garmin payload. For `--out`, the file contains the same JSON envelope as stdout. Choose a new destination: exports refuse to overwrite files. If the user supplied an existing path, preserve it and resolve a new destination before writing. An incomplete export may still be written, but returns nonzero and retains explicit errors; never report it as complete.

## Read-only device and activity access

```bash
garmcli --json devices list
garmcli --json activities list --limit 10
garmcli --json activities download 123456789 --format fit --out ./activity.fit
```

Discover the actual activity ID from `activities list`; the ID above is illustrative. Downloads support FIT, GPX, TCX, KML and CSV. FIT preserves original activity data; activity CSV contains split summaries, not the full measurement stream. Health exports use JSON. The destination directory must already exist; downloads also refuse overwrite.

## Interpret results accurately

- The outer envelope has `schema_version`, `command`, `ok`, `data`, `errors`, and, for history/sync, `complete`. Garmin's inner payload schema remains upstream-controlled. Progress is stderr; stdout is one JSON envelope.
- `available` means successful nonempty JSON, which can still contain missing fields. `empty` means top-level null, `[]` or `{}`. `error` means a failed request. `missing` means no stored response. Zero is data, not absence. Never convert errors or missing measurements into zero.
- Preserve units, metric descriptors, timestamps and device attribution. Exclude nulls and metric-specific sentinel values from calculations only when their meaning is established. Requested record dates are query dates; inspect embedded measurement dates, especially latest-style metrics and sleep nights.
- A 404 or empty response does not prove a watch lacks a feature. Resting HR is accessible through summary, heart-rate or sleep responses; there is no standalone resting-HR command in this tool. Sleep skin-temperature values are deviations from a baseline, not absolute body temperature.
- Exit codes: 0 success including empty; 1 other failure/incomplete history; 2 invalid input; 3 authentication required; 5 rate limited; 130 interrupted. Check both exit status and envelope before claiming success.

## Authentication and boundaries

Use existing gccli authentication. Saved expired tokens can refresh on the next live request. If login is needed within the authorized task, use the normal `gccli auth login` flow and available authorized credentials; never expose passwords or tokens. Never call `gccli auth token` or `gccli auth export`. Account recovery and password/MFA changes require separate explicit authorization.

Keep this skill read-only on Garmin: do not upload activities, create or schedule workouts, edit settings, delete records or call gccli write commands. Local sync and requested file exports are permitted within the user's task. Export only requested data; archives can contain health/profile data and GPS locations. Do not share files externally or create scheduled syncs unless requested.

For unattended work, continue independent authorized offline reads when possible. Do not wait for an absent user or invent credentials, scope or authorization. Report assumptions and blockers, and leave dependent work pending when required information or access is unavailable.
