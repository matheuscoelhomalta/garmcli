# garmcli

A read-only Garmin data CLI built on `gccli`. It saves dated health responses in local SQLite history, resumes interrupted exports, and returns a versioned JSON envelope for agents.

An independent community tool, not affiliated with or endorsed by Garmin. Live access uses [gccli](https://github.com/bpauli/gccli); local history and exports are managed by garmcli.

Requires Python 3.11+ and a working `gccli` installation. Runtime dependencies are Python's standard library. Authentication belongs to `gccli`; this tool does not store passwords or export tokens.

Version 1.0.0 was tested with gccli 1.13.0. The CLI owns its command and envelope contracts; gccli owns Garmin API access and authentication maintenance.

## Install

Install [gccli](https://github.com/bpauli/gccli#installation) and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv tool install git+https://github.com/matheuscoelhomalta/garmcli.git
garmcli --json doctor
```

For a local checkout:

```bash
git clone https://github.com/matheuscoelhomalta/garmcli.git
cd garmcli
uv tool install .
```

If authentication is missing, sign in with `gccli auth login your-email@example.com` and then run `doctor` again. On macOS, use the gccli Keychain backend.

For development only, add `--editable` to the install command. A normal installation copies the package into uv's tool environment and keeps working if the source folder moves.

Live queries require gccli and a Garmin Connect session. Cached queries require neither. The code uses platform-specific local data directories; the CI suite tests Linux and macOS with Python 3.11 and 3.14.

## Agent skill

The companion skill lives in [`skills/garmcli/SKILL.md`](skills/garmcli/SKILL.md). From a persistent checkout, install it for Codex:

```bash
mkdir -p "$HOME/.codex/skills"
ln -s "$PWD/skills/garmcli" "$HOME/.codex/skills/garmcli"
```

The link must point to a checkout that stays in place. The command refuses an existing link or directory; inspect it before changing anything. The skill is configured for automatic invocation and covers resumable sync, offline reads, exports, and read-only access.

## Export and query history

```bash
garmcli --json devices list

garmcli --json sync --from 2026-09-14 --to 2026-10-13

garmcli --json health sleep --from 2026-09-14 --to 2026-10-13 --offline

garmcli --json health heart-rate --from 2026-10-01 --to 2026-10-13 \
  --offline --out ./heart-rate.json

garmcli --json snapshot --date 2026-10-13 --offline

garmcli --json activities list --limit 10
garmcli --json activities download 123456789 --format fit --out ./activity.fit
```

Global options precede the command: `--json`, `--db PATH`, `--gccli PATH`, `--account-id ID`, and `--timeout SECONDS`. An explicit `--db` keeps a dataset in a selected location; the macOS default is `~/Library/Application Support/garmcli/history.sqlite3`.

`health` and `snapshot` query stored history only, even without `--offline`; run `sync` to fetch fresh data. Offline queries do not need Garmin authentication or network access. If a database contains multiple account identities, select one with `--account-id`. Account identities are hashes of the Garmin service domain and normalized account email, not authentication credentials.

## Sync semantics

Dates are strict `YYYY-MM-DD`, inclusive, and represent the Garmin calendar date requested. By default, sync fetches these 15 metric families:

`summary`, `steps`, `heart-rate`, `floors`, `sleep`, `stress`, `respiration`, `spo2`, `hrv`, `body-battery`, `intensity-minutes`, `training-readiness`, `training-status`, `fitness-age`, `max-metrics`.

Select a subset with `--metrics summary,sleep,hrv`. Sync waits 0.5 seconds between network requests by default; adjust with `--delay`. gccli owns HTTP retries and token refresh. Ctrl+C stops the command and preserves each already-committed response. Repeat the exact sync command to resume: available and empty records are skipped, failed records are retried, and missing records are fetched. Use `--refresh` to explicitly fetch completed records again.

Ordinary endpoint errors are recorded and independent requests continue. Authentication failures or rate limits stop the run. A request failure is never stored as a zero or an empty result.

`complete: true` means every requested metric/date has a successful response, including confirmed empty responses. It does **not** mean every date contains measurements. Check counts and record statuses before analysis.

## Stable JSON contract

JSON stdout contains one envelope. Progress is sent to stderr.

`--help` returns standard help text, including when `--json` is selected. `doctor` checks saved credentials and local setup; it does not prove live endpoint reachability. Expired saved tokens can still be refreshed by gccli on the next live request.

```json
{
  "schema_version": "1",
  "command": "health",
  "ok": true,
  "complete": true,
  "data": {},
  "errors": []
}
```

The outer schema is controlled by this CLI. Each history record has `date`, `metric`, `status`, `data`, `error`, `fetched_at`, and `attempts`. The `data` field preserves Garmin's response unchanged; Garmin's inner schema can change independently.

| Status | Meaning |
| --- | --- |
| `available` | A successful nonempty response; individual fields may still be missing |
| `empty` | Successful top-level `null`, `[]`, or `{}` |
| `error` | The request failed; error details are explicit |
| `missing` | No stored result for that metric/date |

Numbers equal to zero remain data. A 404 or an empty response does not establish that a watch lacks the feature. Raw streams may contain nulls and negative sentinel values; use their descriptors and metric-specific semantics before calculating statistics. Preserve units, timestamps and device attribution.

The requested date is not always the measurement date: some Garmin endpoints return latest-style values or nested historical timestamps. Sleep's calendar date generally identifies the night ending on that day. Check embedded dates and timestamps. Skin temperature in sleep exports is deviation from a baseline, not absolute body temperature. Dedicated Body Battery data may have fewer points than the Body Battery series included in the stress response.

Resting HR is available in `summary`, `heart-rate`, and `sleep`. Standalone resting-HR and race-prediction commands are outside this CLI's v1 surface.

| Exit | Meaning |
| --- | --- |
| 0 | Success, including confirmed empty results |
| 1 | Other failure or incomplete query/export |
| 2 | Invalid input |
| 3 | Authentication required |
| 5 | Rate limited |
| 130 | Interrupted sync |

File exports refuse to overwrite existing files. The database is account-scoped and commits each response atomically. Local history and exported files can contain personal health data and GPS locations; the application does not export authentication tokens.

## Development and checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use synthetic data and a fake gccli. Personal live-test files are stored separately from this source project.

Contributions should include tests for changed behavior. Use synthetic fixtures when reporting bugs; do not include personal health exports, database files, passwords or tokens in issues or pull requests.

## License

[MIT](LICENSE). gccli is a separately installed upstream dependency with its own license.
