import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from garmcli.backend import BackendError, GarminBackend
from garmcli.contracts import METRICS, is_empty, make_record, parse_dates
from garmcli.store import HistoryStore


class ContractTests(unittest.TestCase):
    def test_dates_strict_inclusive_and_calendar_valid(self):
        self.assertEqual(parse_dates("2024-02-28", "2024-03-01"), ["2024-02-28", "2024-02-29", "2024-03-01"])
        for first, last in [
            ("today", "2024-03-01"), ("2024-2-28", "2024-03-01"),
            ("2023-02-29", "2024-03-01"), ("2024-03-02", "2024-03-01"),
            ("２０２４-02-28", "2024-03-01"),
        ]:
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                parse_dates(first, last)

    def test_only_explicit_top_level_absence_is_empty(self):
        for payload in (None, {}, []):
            self.assertTrue(is_empty(payload))
        for payload in (0, False, "", {"steps": 0}, {"samples": []}, [0]):
            self.assertFalse(is_empty(payload))

    def test_records_have_consistent_fields_and_no_external_message(self):
        missing = make_record("2026-01-01", "summary", "missing")
        self.assertEqual(set(missing), {"date", "metric", "status", "data", "error", "fetched_at", "attempts"})
        self.assertIsNone(missing["fetched_at"])
        self.assertEqual(missing["attempts"], 0)
        payload = {"steps": 0, "nested": [None, {"source": "synthetic"}]}
        available = make_record("2026-01-01", "summary", "available", payload)
        self.assertIs(available["data"], payload)
        error = make_record("2026-01-01", "summary", "error", error={
            "code": "not_found", "status": 404, "message": "token=SECRET synthetic@example.com <html>BODY</html>",
            "token": "SECRET",
        })["error"]
        self.assertEqual(error, {"code": "not_found", "message": "Garmin request failed (HTTP 404).", "status": 404})
        self.assertEqual(make_record("2026-01-01", "summary", "error", error={"code": []})["error"]["code"], "upstream_error")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.fixture = self.directory / "response.json"
        self.log = self.directory / "calls.jsonl"
        self.executable = self.directory / "fake-gccli"
        self.executable.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys, time
from pathlib import Path
argv = sys.argv[1:]
with open(os.environ["FAKE_GCCLI_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
response = json.loads(Path(os.environ["FAKE_GCCLI_RESPONSE"]).read_text())
time.sleep(response.get("sleep", 0))
if "--output" in argv and "download" in response:
    destination = Path(argv[argv.index("--output") + 1])
    destination.write_bytes(response["download"].encode())
    if response.get("race_destination"):
        Path(response["race_destination"]).write_text("other owner")
stdout = response.get("stdout", json.dumps(response.get("data")))
if response.get("reflect_account"):
    selected = argv[argv.index("--account") + 1] if "--account" in argv else os.environ.get("GCCLI_ACCOUNT")
    stdout = json.dumps({"selected_account": selected})
sys.stdout.write(stdout)
sys.stderr.write(response.get("stderr", ""))
sys.exit(response.get("exit", 0))
''')
        self.executable.chmod(0o700)
        self.environment = patch.dict(os.environ, {
            "FAKE_GCCLI_LOG": str(self.log), "FAKE_GCCLI_RESPONSE": str(self.fixture),
        })
        self.environment.start()
        self.backend = GarminBackend(str(self.executable), timeout=2)
        self.respond(data={"value": 0})

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def respond(self, **response):
        self.fixture.write_text(json.dumps(response))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_exact_read_only_command_allowlist_and_raw_payload(self):
        payload = {"synthetic": {"zero": 0, "samples": [1, None]}, "measurementDate": "2025-12-31"}
        self.respond(data=payload)
        for metric, argv in METRICS.items():
            self.assertEqual(self.backend.fetch(metric, "2026-01-01"), payload)
            self.assertEqual(self.calls()[-1], [*argv, "2026-01-01", "--json"])
        self.backend.devices()
        self.assertEqual(self.calls()[-1], ["devices", "list", "--json"])
        self.backend.activities(1000)
        self.assertEqual(self.calls()[-1], ["activities", "list", "--limit", "1000", "--json"])
        self.respond(stdout="gccli 1.13.0\n")
        self.assertEqual(self.backend.version(), "gccli 1.13.0")
        self.assertEqual(self.calls()[-1], ["--version"])
        self.assertEqual(len(METRICS), 15)
        self.assertNotIn("rhr", METRICS)
        self.assertNotIn("race-predictions", METRICS)

    def test_invalid_inputs_never_start_a_subprocess(self):
        operations = [
            lambda: self.backend.fetch("delete", "2026-01-01"),
            lambda: self.backend.fetch("summary; echo unsafe", "2026-01-01"),
            lambda: self.backend.fetch("summary", "today"),
            lambda: self.backend.fetch([], "2026-01-01"),
            *[lambda value=value: self.backend.activities(value) for value in (0, -1, 1001, True, "2")],
            *[lambda value=value: self.backend.download(value, "fit", self.directory / "x.fit") for value in ("0", "-1", "x", "1;echo", "01x")],
            lambda: self.backend.download("1", "exe", self.directory / "x.fit"),
            lambda: self.backend.download("1", "fit", self.directory / "missing" / "x.fit"),
        ]
        for operation in operations:
            with self.assertRaises(BackendError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(self.calls(), [])
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(BackendError):
                GarminBackend(str(self.executable), value)

    def test_auth_hash_is_normalized_service_scoped_and_cached(self):
        self.respond(data={
            "domain": "Garmin.COM", "email": "  Synthetic@Example.COM ", "expired": False,
            "expires_at": "2026-01-02T00:00:00+00:00", "access_token": "SECRET",
        })
        status = self.backend.auth_status()
        expected = hashlib.sha256(b"garmin.com:synthetic@example.com").hexdigest()
        self.assertEqual(status["account_id"], expected)
        self.assertEqual(self.backend.account_id(), expected)
        self.assertEqual(self.calls(), [["auth", "status", "--json"]])
        self.assertNotIn("synthetic@", json.dumps(status).lower())
        self.assertNotIn("SECRET", json.dumps(status))
        status["account_id"] = "mutated"
        self.assertEqual(self.backend.account_id(), expected)
        other = GarminBackend(str(self.executable))
        self.respond(data={"domain": "garmin.cn", "email": "synthetic@example.com", "expired": False})
        self.assertNotEqual(other.account_id(), expected)

    def test_missing_auth_fails_and_stored_expired_credentials_can_refresh(self):
        for payload in ({}, []):
            self.respond(data=payload)
            with self.subTest(payload=payload), self.assertRaises(BackendError) as raised:
                GarminBackend(str(self.executable)).account_id()
            self.assertIn(raised.exception.code, {"auth_required", "auth_expired"})
        self.respond(data={"domain": "garmin.com", "email": "synthetic@example.com", "expired": True})
        backend = GarminBackend(str(self.executable))
        status = backend.auth_status()
        self.assertTrue(status["expired"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(backend.account_id(), status["account_id"])

    def test_resolved_identity_pins_requests_after_default_account_changes(self):
        self.respond(data={"domain": "garmin.com", "email": "First@Example.com", "expired": False})
        identity = self.backend.account_id()
        with patch.dict(os.environ, {"GCCLI_ACCOUNT": "second@example.com"}):
            self.respond(reflect_account=True)
            self.assertEqual(self.backend.fetch("summary", "2026-01-01"), {"selected_account": "first@example.com"})
            self.assertEqual(self.calls()[-1][-2:], ["--account", "first@example.com"])
            self.assertEqual(self.backend.devices(), {"selected_account": "first@example.com"})
            self.assertEqual(self.backend.activities(1), {"selected_account": "first@example.com"})
            self.respond(data={}, download="synthetic activity")
            self.backend.download("1", "fit", self.directory / "pinned.fit")
            self.assertEqual(self.calls()[-1][-2:], ["--account", "first@example.com"])
            self.assertEqual(self.backend.account_id(), identity)
        self.assertEqual(sum(argv[:2] == ["auth", "status"] for argv in self.calls()), 1)

    def test_upstream_errors_are_structured_sanitized_and_not_retried(self):
        for code, status, exit_code in (("not_found", 404, 1), ("auth_required", 401, 3), ("rate_limited", 429, 5)):
            self.respond(exit=exit_code, stderr=json.dumps({"error": {
                "code": code, "status": status, "message": "Authorization: SECRET synthetic@example.com <html>BODY</html>",
            }}))
            before = len(self.calls())
            with self.assertRaises(BackendError) as raised:
                self.backend.fetch("summary", "2026-01-01")
            error = raised.exception.as_dict()
            self.assertEqual(error["code"], code)
            self.assertEqual(error["status"], status)
            self.assertNotIn("SECRET", json.dumps(error))
            self.assertNotIn("BODY", json.dumps(error))
            self.assertNotIn("example.com", json.dumps(error))
            self.assertEqual(len(self.calls()), before + 1)
        self.respond(exit=1, stderr="password=SECRET synthetic@example.com")
        with self.assertRaises(BackendError) as raised:
            self.backend.devices()
        self.assertEqual(raised.exception.as_dict(), {"code": "backend_error", "message": "Garmin request failed."})

    def test_invalid_json_timeout_and_missing_executable_are_safe(self):
        for stdout in ("not json SECRET", '{"sample": NaN}', '{"sample": Infinity}'):
            self.respond(stdout=stdout)
            with self.assertRaises(BackendError) as raised:
                self.backend.devices()
            self.assertEqual(raised.exception.code, "invalid_json")
            self.assertNotIn("SECRET", raised.exception.message)
        self.respond(sleep=0.3, stdout="SECRET")
        with self.assertRaises(BackendError) as raised:
            GarminBackend(str(self.executable), timeout=0.02).devices()
        self.assertEqual(raised.exception.code, "timeout")
        with self.assertRaises(BackendError) as raised:
            GarminBackend(str(self.directory / "no-binary")).version()
        self.assertEqual(raised.exception.code, "executable_missing")

    def test_download_preserves_existing_files_and_publishes_new_file(self):
        destination = self.directory / "activity.fit"
        destination.write_bytes(b"original")
        with self.assertRaises(BackendError):
            self.backend.download("123", "fit", destination)
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(self.calls(), [])
        destination.unlink()
        self.respond(data={}, download="synthetic activity")
        result = self.backend.download("123", "fit", destination)
        self.assertEqual(result, {"path": str(destination), "bytes": 18, "activity_id": "123", "format": "fit"})
        self.assertEqual(destination.read_bytes(), b"synthetic activity")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        argv = self.calls()[-1]
        self.assertEqual(argv[:5], ["activity", "download", "123", "--format", "fit"])
        self.assertEqual(argv[-1], "--json")
        self.assertNotEqual(argv[argv.index("--output") + 1], str(destination))
        self.assertEqual(list(self.directory.glob(".garmcli-*")), [])

    def test_download_race_and_missing_output_are_failures(self):
        destination = self.directory / "race.fit"
        self.respond(data={}, download="new data", race_destination=str(destination))
        with self.assertRaises(BackendError) as raised:
            self.backend.download("1", "fit", destination)
        self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(destination.read_text(), "other owner")
        for response in ({"data": {}}, {"data": {}, "download": ""}):
            self.respond(**response)
            missing = self.directory / "missing.fit"
            with self.assertRaises(BackendError) as raised:
                self.backend.download("1", "fit", missing)
            self.assertEqual(raised.exception.code, "download_failed")
            self.assertFalse(missing.exists())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "private-history" / "history.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_empty_error_available_and_account_isolation(self):
        with HistoryStore(self.path) as store:
            missing = store.get("account-one", "summary", "2026-01-01")
            self.assertEqual(missing["status"], "missing")
            self.assertEqual(missing["attempts"], 0)
            self.assertIsNone(missing["fetched_at"])
            for offset, payload in enumerate((None, {}, []), 1):
                self.assertEqual(store.save("account-one", "summary", f"2026-01-0{offset}", payload)["status"], "empty")
            data = {"steps": 0, "nested": [{"source": "synthetic"}, None], "measurementDate": "2025-12-31"}
            saved = store.save("account-one", "summary", "2026-01-04", data)
            self.assertEqual(saved["status"], "available")
            self.assertEqual(saved["data"], data)
            self.assertEqual(saved["attempts"], 1)
            self.assertTrue(saved["fetched_at"].endswith("+00:00"))
            error = store.save("account-one", "sleep", "2026-01-04", error={"code": "not_found", "status": 404, "message": "SECRET"})
            self.assertEqual(error["status"], "error")
            self.assertEqual(error["error"]["status"], 404)
            self.assertNotIn("SECRET", json.dumps(error))
            self.assertEqual(store.get("account-two", "summary", "2026-01-04")["status"], "missing")
            store.save("account-two", "summary", "2026-01-04", {"steps": 5})
            self.assertEqual(store.get("account-one", "summary", "2026-01-04")["data"], data)
            self.assertEqual(store.accounts(), ["account-one", "account-two"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_each_save_commits_and_retries_increment_attempts(self):
        store = HistoryStore(self.path)
        failed = store.save("account-one", "hrv", "2026-01-01", error={"code": "server_error", "status": 500})
        self.assertEqual(failed["attempts"], 1)
        # A separate connection sees the completed request before close/sync end.
        with HistoryStore(self.path) as reopened:
            self.assertEqual(reopened.get("account-one", "hrv", "2026-01-01")["status"], "error")
        recovered = store.save("account-one", "hrv", "2026-01-01", {"value": 0})
        self.assertEqual(recovered["status"], "available")
        self.assertEqual(recovered["attempts"], 2)
        self.assertIsNone(recovered["error"])
        store.close()
        with HistoryStore(self.path) as reopened:
            self.assertEqual(reopened.get("account-one", "hrv", "2026-01-01"), recovered)
            self.assertEqual(reopened.get("account-one", "hrv", "2026-01-02")["status"], "missing")

    def test_unserializable_save_and_transaction_failure_preserve_prior_record(self):
        with HistoryStore(self.path) as store:
            original = store.save("a", "summary", "2026-01-01", {"value": 1})
            for payload in ({"value": object()}, {"value": float("nan")}):
                with self.assertRaises((TypeError, ValueError)):
                    store.save("a", "summary", "2026-01-01", payload)
                self.assertEqual(store.get("a", "summary", "2026-01-01"), original)
            connection = sqlite3.connect(self.path)
            connection.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
            connection.commit()
            connection.close()
            with self.assertRaises(sqlite3.IntegrityError):
                store.save("a", "summary", "2026-01-01", {"value": 2})
            self.assertEqual(store.get("a", "summary", "2026-01-01"), original)

    def test_future_and_foreign_files_are_refused_without_reset(self):
        with HistoryStore(self.path) as store:
            store.save("a", "summary", "2026-01-01", {"synthetic": True})
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "newer"):
            HistoryStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        foreign = self.directory / "foreign.sqlite3"
        connection = sqlite3.connect(foreign)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
        connection.close()
        original = foreign.read_bytes()
        with self.assertRaisesRegex(ValueError, "unrelated"):
            HistoryStore(foreign)
        self.assertEqual(foreign.read_bytes(), original)
        for name, contents in (("text.txt", b"unrelated data"), ("empty.db", b"")):
            file = self.directory / name
            file.write_bytes(contents)
            with self.assertRaises(ValueError):
                HistoryStore(file)
            self.assertEqual(file.read_bytes(), contents)
        link = self.directory / "linked.db"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            HistoryStore(link)

    def test_interrupted_process_leaves_completed_record_committed(self):
        program = r'''
import sys
from pathlib import Path
from garmcli.store import HistoryStore
store = HistoryStore(Path(sys.argv[1]))
store.save("synthetic-account", "summary", "2026-01-01", {"value": 0})
raise KeyboardInterrupt()
'''
        result = subprocess.run([sys.executable, "-c", program, str(self.path)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        with HistoryStore(self.path) as store:
            self.assertEqual(store.get("synthetic-account", "summary", "2026-01-01")["status"], "available")
            self.assertEqual(store.get("synthetic-account", "summary", "2026-01-02")["status"], "missing")


if __name__ == "__main__":
    unittest.main()
