# Contributing to garmcli

garmcli is a read-only Garmin Connect CLI using the [garminconnect](https://github.com/cyberjunky/python-garminconnect) Python library. It owns authentication, the fixed read-only request allowlist, local SQLite history, resumable synchronization and the outer JSON contract.

## Report a bug or propose a change

Check the [command reference](docs/reference.md) and [existing issues](https://github.com/matheuscoelhomalta/garmcli/issues) first. For a bug report, include:

- Your garmcli and backend-library versions, operating system and Python version.
- The command, expected behavior, actual behavior and exit code.
- A minimal synthetic example that reproduces the problem.

Find the installed versions with:

```bash
python3 -c 'from importlib.metadata import version; print(version("garmcli"))'
garmcli --json doctor
```

Run the Python command inside the environment containing garmcli. For a uv tool installation, use `uv tool list` to see the installed package version.

Do not attach personal Garmin exports, GPS tracks, databases, account identifiers, passwords or tokens to public issues or pull requests. Replace real records with synthetic data. Never export or include authentication credentials in a bug report. Review and redact account identity and local paths from diagnostics before sharing.

For a proposed behavior change, explain the user need and expected result before investing in a large implementation. Garmin record creation, editing, uploading, scheduling and deletion are outside this CLI's read-only scope.

## Set up and test locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Run from the root of this checkout:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --editable .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/garmcli --help
```

These commands target macOS and Linux, matching the CI platforms. The test suite uses synthetic data and a fake backend; it requires no Garmin account, live API access or personal health records. Runtime libraries install with the package. Live operations use `garminconnect==0.3.18` and a secure OS session store through `keyring`; there is no plaintext fallback. Offline reads must work without importing these libraries, accessing a keyring, or making network requests.

## Submit a pull request

Keep changes focused and preserve unrelated behavior. Explain the problem, resulting behavior and validation in the pull request description. Add regression coverage for meaningful behavior changes, using synthetic fixtures.

Preserve these contracts when changing implementation:

- The Garmin data-request boundary remains a fixed read-only GET allowlist. Authentication may perform the requests required to sign in and refresh sessions.
- Passwords and MFA codes use hidden prompts only; session secrets remain in macOS Keychain or Linux Secret Service and never enter JSON output, logs or plaintext files.
- Existing SQLite history and normalized-email/domain account IDs remain compatible.
- Completed sync responses survive interruption and can be resumed.
- Account identities stay isolated in local history.
- Missing records, empty responses, errors and valid zero values remain distinct.
- JSON stdout stays parseable; progress belongs on stderr.
- File exports preserve existing destinations.

For documentation changes, check examples against `garmcli --help` and subcommand help, and verify links. Describe any upstream-dependent behavior as such. Use the supported Python 3.12+ versions on Linux and macOS. Live endpoint checks are separate from the synthetic suite and local `doctor` diagnostics; do not claim live verification from either.

Contributions are covered by the project's [MIT license](LICENSE).
