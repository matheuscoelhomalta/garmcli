# garmcli — Garmin Connect CLI for health history and exports

Export Garmin Connect health data to JSON, keep a local SQLite history, and query it offline. **garmcli** is a read-only command-line tool for personal analysis, scripts, and AI agents, using the [garminconnect](https://github.com/cyberjunky/python-garminconnect) Python library for Garmin access and its own secure local session store.

[PyPI](https://pypi.org/project/garmcli/) · [Releases](https://github.com/matheuscoelhomalta/garmcli/releases) · [CI](https://github.com/matheuscoelhomalta/garmcli/actions/workflows/ci.yml) · [Reference](https://github.com/matheuscoelhomalta/garmcli/blob/main/docs/reference.md) · [MIT license](https://github.com/matheuscoelhomalta/garmcli/blob/main/LICENSE)

- Sync 15 health metric families, including sleep, heart rate, HRV, stress, Body Battery, and training readiness.
- Resume interrupted syncs without fetching successful metric/date pairs again.
- Read cached history and export JSON without Garmin authentication or network access.
- List devices and activities; download activities as FIT, GPX, TCX, KML, or CSV.
- Parse a versioned JSON envelope with explicit completeness, missing data, and errors.

An independent community tool, not affiliated with or endorsed by Garmin. garmcli does not upload, edit, or delete Garmin records.

## Install

**2.0.0** installs its Garmin Python libraries automatically and requires no
separate Garmin CLI. Existing 1.0.0 users should read the migration notes below.

### Homebrew

```bash
brew install matheuscoelhomalta/tap/garmcli
garmcli --help
```

Homebrew installs Python and the required libraries. For an existing Homebrew
installation, run `brew update` followed by `brew upgrade matheuscoelhomalta/tap/garmcli`.

### PyPI with uv

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv tool install --python 3.12 garmcli==2.0.0
garmcli --help
```

If garmcli is already installed as a uv tool, add `--reinstall` to replace that
installation. The package installs its runtime libraries
automatically, including `garminconnect==0.3.18` and `keyring`. It does not need
a separate Garmin CLI executable.

For a one-off command:

```bash
uvx --python 3.12 garmcli@2.0.0 --help
```

## Quick start

### 1. Authenticate and check setup

Sign in with your Garmin Connect email:

```bash
garmcli auth login your-email@example.com
garmcli --json doctor
```

Password and MFA code prompts are hidden; neither is accepted through command arguments or environment variables. Sessions use macOS Keychain or Linux Secret Service through `keyring`, with no plaintext fallback. Use `--domain garmin.cn` on `auth login` for Garmin China; the default is `garmin.com`.

`garmcli --json auth status` reports local session status. `garmcli auth logout` removes only garmcli's local session, preserving Garmin records and SQLite history. No command exports raw authentication tokens. `doctor` checks local setup and session availability; it does not test live endpoint reachability. Saved expired tokens may refresh on the next live request.

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

**`health` and `snapshot` always read local history**, even without `--offline`; run `sync` to fetch fresh data. These queries need neither a Garmin session nor the live backend libraries; they perform no network access. Exports refuse to overwrite existing files; choose a new path. An incomplete export can still write a file and exits nonzero: check the exit status, `ok`, `complete`, and `errors` before analysis.

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

Global options go **before** the command: `--json`, `--db PATH`, `--account-id ID`, and `--timeout SECONDS`. Use `--db ./history.sqlite3` consistently to select a dataset. The macOS default is `~/Library/Application Support/garmcli/history.sqlite3`; if a database contains multiple accounts, select one with `--account-id`.

See the [command and data reference](https://github.com/matheuscoelhomalta/garmcli/blob/main/docs/reference.md) for sync behavior, JSON fields, exit codes, payload caveats, privacy, and troubleshooting. Use `garmcli --help` or subcommand help for accepted options. With `--json`, command output uses one JSON envelope on stdout and progress goes to stderr. Help remains plain text even with `--json`.

## Companion skill for Codex

The [garmcli skill](https://github.com/matheuscoelhomalta/garmcli/blob/main/skills/garmcli/SKILL.md) guides agents through read-only sync, offline queries, exports, and data interpretation. It requires the CLI installation above.

Homebrew installs the skill under `$(brew --prefix garmcli)/share/garmcli/skills/garmcli`.
Copy it into Codex's skill directory:

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

The 2.0.0 runtime requires Python 3.12+ on macOS or Linux and pins `garminconnect==0.3.18`. garmcli controls authentication, its fixed read-only request allowlist, local history, and outer JSON schema version `1`. Garmin's inner response schemas may change independently.

When upgrading from 1.0.0, sign in again with `garmcli auth login`. Existing SQLite history needs no migration or reset. The same normalized email and Garmin domain retain the same account ID. The old `--gccli` global option is removed.

Local history is account-scoped and commits each response separately. Databases and exports can contain personal health data and GPS locations. Keep them private; use synthetic fixtures in bug reports and never attach passwords or tokens.

## Development

See [Contributing](https://github.com/matheuscoelhomalta/garmcli/blob/main/CONTRIBUTING.md) for an isolated development environment, test commands, bug reports, and pull requests. Tests use synthetic data and fake backends; no Garmin account is required.

## License

[MIT](https://github.com/matheuscoelhomalta/garmcli/blob/main/LICENSE). Runtime dependencies have their own licenses.
