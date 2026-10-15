# Contributing to garmcli

garmcli is a read-only Garmin Connect CLI built on [gccli](https://github.com/bpauli/gccli). It owns local SQLite history, resumable synchronization and the outer JSON contract. gccli owns authentication and Garmin API access.

## Report a bug or propose a change

Check the [command reference](docs/reference.md) and [existing issues](https://github.com/matheuscoelhomalta/garmcli/issues) first. For a bug report, include:

- Your garmcli and gccli versions, operating system and Python version.
- The command, expected behavior, actual behavior and exit code.
- A minimal synthetic example that reproduces the problem.

Find the installed versions with:

```bash
python3 -c 'from importlib.metadata import version; print(version("garmcli"))'
gccli --version
```

Run the Python command inside the environment containing garmcli. For a uv tool installation, use `uv tool list` to see the installed package version.

Do not attach personal Garmin exports, GPS tracks, databases, account identifiers, passwords or tokens to public issues or pull requests. Replace real records with synthetic data. Do not run `gccli auth token` or export authentication credentials for a bug report.

For a proposed behavior change, explain the user need and expected result before investing in a large implementation. Garmin record creation, editing, uploading, scheduling and deletion are outside this CLI's read-only scope.

## Set up and test locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
git clone https://github.com/matheuscoelhomalta/garmcli.git
cd garmcli
uv venv .venv
uv pip install --python .venv/bin/python --editable .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/garmcli --help
```

These commands target macOS and Linux, matching the CI platforms. The test suite uses synthetic data and a fake backend; it requires no Garmin account, live API access or personal health records. A separately installed gccli is needed only for live operations.

## Submit a pull request

Keep changes focused and preserve unrelated behavior. Explain the problem, resulting behavior and validation in the pull request description. Add regression coverage for meaningful behavior changes, using synthetic fixtures.

Preserve these contracts when changing implementation:

- The Garmin command boundary remains read-only.
- Completed sync responses survive interruption and can be resumed.
- Account identities stay isolated in local history.
- Missing records, empty responses, errors and valid zero values remain distinct.
- JSON stdout stays parseable; progress belongs on stderr.
- File exports preserve existing destinations.

For documentation changes, check examples against `garmcli --help` and subcommand help, and verify links. Describe any upstream-dependent behavior as such. CI runs the synthetic suite on Linux and macOS with Python 3.11 and 3.14.

Contributions are covered by the project's [MIT license](LICENSE).
