import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from garmcli.contracts import is_empty, make_record, parse_dates
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
