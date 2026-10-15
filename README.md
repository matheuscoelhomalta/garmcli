# garmcli — Garmin Connect CLI for health history and exports

Export Garmin Connect health data to JSON, keep a local SQLite history, and query it offline. **garmcli** is a read-only command-line tool for personal analysis, scripts, and AI agents, built on [gccli](https://github.com/bpauli/gccli) for Garmin access and authentication.

[PyPI](https://pypi.org/project/garmcli/) · [Releases](https://github.com/matheuscoelhomalta/garmcli/releases) · [CI](https://github.com/matheuscoelhomalta/garmcli/actions/workflows/ci.yml) · [Reference](https://github.com/matheuscoelhomalta/garmcli/blob/main/docs/reference.md) · [MIT license](https://github.com/matheuscoelhomalta/garmcli/blob/main/LICENSE)

- Sync 15 health metric families, including sleep, heart rate, HRV, stress, Body Battery, and training readiness.
- Resume interrupted syncs without fetching successful metric/date pairs again.
- Read cached history and export JSON without Garmin authentication or network access.
- List devices and activities; download activities as FIT, GPX, TCX, KML, or CSV.
- Parse a versioned JSON envelope with explicit completeness, missing data, and errors.

An independent community tool, not affiliated with or endorsed by Garmin. garmcli does not upload, edit, or delete Garmin records.

## Install

### Homebrew

Installs Python and gccli as dependencies:

```bash
brew install matheuscoelhomalta/tap/garmcli
garmcli --help
```

### PyPI with uv

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and a separately installed [gccli](https://github.com/bpauli/gccli#installation) on your PATH:

```bash
uv tool install garmcli==1.0.0
garmcli --help
```

For a one-off command: `uvx garmcli@1.0.0 --help`. garmcli itself uses only Python's standard library; its PyPI package does not install gccli.

## Quick start

### 1. Authenticate and check setup

If you do not already have a gccli session, sign in with your Garmin Connect email:

```bash
gccli auth login your-email@example.com
garmcli --json doctor
```

Authentication is managed by gccli; garmcli does not store passwords or export tokens. On macOS, use gccli's Keychain backend. `doctor` checks local setup and saved credentials; it does not test live endpoint reachability. Saved expired tokens may refresh on the next live request.

### 2. Sync health history

Replace the example dates with your desired range. Dates use `YYYY-MM-DD` and both endpoints are included.

```bash
garmcli --json sync --from 2026-09-14 --to 2026-10-13 \
  --metrics summary,sleep,hrv
```

Repeat the same command with the same database and account to resume after interruption: successful responses, including empty ones, are skipped; failed and missing requests are retried or fetched. Use `--refresh` when you explicitly want to fetch completed records again. Omit `--metrics` to sync all 15 metric families.

### 3. Query and export offline

```bash
garmcli --json health sleep --from 2026-09-14 --to 2026-10-13 --offline

garmcli --json health hrv --from 2026-09-14 --to 2026-10-13 \
  --offline --out ./hrv.json

garmcli --json snapshot --date 2026-10-13 \
  --metrics summary,sleep,hrv --offline
```

**`health` and `snapshot` always read local history**, even without `--offline`; run `sync` to fetch fresh data. These queries need neither gccli nor a Garmin session. Exports refuse to overwrite existing files; choose a new path. An incomplete export can still write a file and exits nonzero: check the exit status, `ok`, `complete`, and `errors` before analysis.

**Missing data is not zero.** Records distinguish `available`, `empty`, `error`, and `missing`; zero remains a measurement. `complete: true` includes confirmed empty responses and does not mean measurements exist on every day. Raw Garmin payloads can contain missing fields, nulls, and sentinel values; see the [data interpretation reference](https://github.com/matheuscoelhomalta/garmcli/blob/main/docs/reference.md).

## Supported data and commands

Health metric identifiers:

```text
summary           steps               heart-rate         floors
sleep             stress              respiration        spo2
hrv               body-battery        intensity-minutes  training-readiness
training-status   fitness-age         max-metrics
```

Metric availability and payload contents depend on Garmin's responses. Empty data or a 404 does not establish that a watch lacks a feature. Resting heart rate is available through `summary`, `heart-rate`, and `sleep`; there is no standalone resting-HR command.

Device and activity commands use live Garmin access:

```bash
garmcli --json devices list
garmcli --json activities list --limit 10
garmcli --json activities download 123456789 --format fit --out ./activity.fit
```

Replace `123456789` with an ID returned by `activities list`. The output directory must already exist. Health exports use JSON.

Global options go **before** the command: `--json`, `--db PATH`, `--gccli PATH`, `--account-id ID`, and `--timeout SECONDS`. Use `--db ./history.sqlite3` consistently to select a dataset. The macOS default is `~/Library/Application Support/garmcli/history.sqlite3`; if a database contains multiple accounts, select one with `--account-id`.

See the [command and data reference](https://github.com/matheuscoelhomalta/garmcli/blob/main/docs/reference.md) for sync behavior, JSON fields, exit codes, payload caveats, privacy, and troubleshooting. Use `garmcli --help` or subcommand help for accepted options. With `--json`, command output uses one JSON envelope on stdout and progress goes to stderr. Help remains plain text even with `--json`.

## Companion skill for Codex

The [garmcli skill](https://github.com/matheuscoelhomalta/garmcli/blob/main/skills/garmcli/SKILL.md) guides agents through read-only sync, offline queries, exports, and data interpretation. It requires the CLI installation above.

Homebrew installs the skill under `$(brew --prefix garmcli)/share/garmcli/skills/garmcli`. Copy it into Codex's skill directory:

```bash
skill_source="$(brew --prefix garmcli)/share/garmcli/skills/garmcli"
skill_destination="$HOME/.codex/skills/garmcli"
if [ -e "$skill_destination" ] || [ -L "$skill_destination" ]; then
  printf '%s\n' "Skill destination already exists; inspect it before changing it."
else
  mkdir -p "$HOME/.codex/skills"
  cp -R "$skill_source" "$skill_destination"
fi
```

Alternatively, from the root of a persistent source checkout, create a link:

```bash
skill_destination="$HOME/.codex/skills/garmcli"
if [ -e "$skill_destination" ] || [ -L "$skill_destination" ]; then
  printf '%s\n' "Skill destination already exists; inspect it before changing it."
else
  mkdir -p "$HOME/.codex/skills"
  ln -s "$PWD/skills/garmcli" "$skill_destination"
fi
```

Both commands leave an existing destination untouched, including dangling links. Keep a linked checkout in place; a copied skill must be recopied when you want to update it.

## Compatibility and privacy

Version 1.0.0 was tested with gccli 1.13.0. CI runs synthetic tests on Linux and macOS with Python 3.11 and 3.14. gccli maintains authentication and Garmin API access; garmcli controls its command interface, local history, and outer JSON schema. Garmin's inner response schemas may change independently.

Local history is account-scoped and commits each response separately. Databases and exports can contain personal health data and GPS locations. Keep them private; use synthetic fixtures in bug reports and never attach passwords or tokens.

## Development

See [Contributing](https://github.com/matheuscoelhomalta/garmcli/blob/main/CONTRIBUTING.md) for an isolated development environment, test commands, bug reports, and pull requests. Tests use synthetic data and a fake gccli; no Garmin account is required.

## License

[MIT](https://github.com/matheuscoelhomalta/garmcli/blob/main/LICENSE). gccli is a separately installed upstream dependency with its own license.
