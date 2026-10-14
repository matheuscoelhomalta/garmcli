"""Synthetic command workflows; no Garmin account or personal data required."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from garmcli.backend import BackendError
from garmcli.cli import main
from garmcli.contracts import METRICS
from garmcli.store import HistoryStore


ACCOUNT = "a" * 64
OTHER_ACCOUNT = "b" * 64


class CLIWorkflows(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "history.sqlite3"
        self.live = Mock()
        self.live.account_id.return_value = ACCOUNT
        self.live.auth_status.return_value = {"authenticated": True, "expired": False, "domain": "garmin.com", "account_id": ACCOUNT}
        self.live.version.return_value = "gccli 1.13.0"
        self.live.devices.return_value = [{"deviceId": 123, "model": "Synthetic device"}]
        self.live.activities.return_value = [{"activityId": 456}]

    def invoke(self, *arguments, live=None):
        out, err = io.StringIO(), io.StringIO()
        with patch("garmcli.cli.GarminBackend", return_value=live or self.live) as constructor:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--json", "--db", str(self.db), *arguments])
        result = json.loads(out.getvalue())
        self.assertEqual(result["schema_version"], "1")
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["errors"], list)
        return code, result, err.getvalue(), constructor

    def save(self, metric, date, data=None, error=None, account=ACCOUNT):
        store = HistoryStore(self.db)
        try:
            return store.save(account, metric, date, data=data, error=error)
        finally:
            store.close()

    def record(self, metric, date):
        store = HistoryStore(self.db)
        try:
            return store.get(ACCOUNT, metric, date)
        finally:
            store.close()

    def sync(self, *extra):
        return self.invoke("sync", "--from", "2026-01-01", "--to", "2026-01-02",
                           "--metrics", "summary,steps", "--delay", "0", *extra)

    def test_inclusive_order_raw_payload_and_resume(self):
        self.live.fetch.side_effect = [{"zero": 0, "raw": [1, 2]}, {}, [], None]
        code, result, progress, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["data"]["counts"], {
            "requested": 4, "fetched": 4, "skipped": 0,
            "available": 1, "empty": 3, "errors": 0, "missing": 0})
        self.assertEqual(self.live.fetch.call_args_list,
                         [unittest.mock.call("summary", "2026-01-01"),
                          unittest.mock.call("steps", "2026-01-01"),
                          unittest.mock.call("summary", "2026-01-02"),
                          unittest.mock.call("steps", "2026-01-02")])
        self.assertEqual(self.record("summary", "2026-01-01")["data"], {"zero": 0, "raw": [1, 2]})
        self.assertIn("2026-01-01 summary: available", progress)
        self.live.fetch.reset_mock()
        code, result, _, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["counts"]["skipped"], 4)
        self.live.fetch.assert_not_called()

    def test_refresh_refetches_completed(self):
        self.live.fetch.return_value = {}
        self.sync()
        self.live.fetch.reset_mock()
        code, result, _, _ = self.sync("--refresh")
        self.assertEqual(code, 0)
        self.assertEqual(self.live.fetch.call_count, 4)
        self.assertEqual(self.record("summary", "2026-01-01")["attempts"], 2)

    def test_ordinary_error_continues_then_resume_retries_only_error(self):
        self.live.fetch.side_effect = [BackendError("not_found", "No data was returned.", 404), {}, {}, {}]
        code, result, _, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["data"]["counts"]["errors"], 1)
        self.assertEqual(result["data"]["counts"]["empty"], 3)
        self.assertEqual(self.record("summary", "2026-01-01")["status"], "error")
        self.live.fetch.reset_mock()
        self.live.fetch.side_effect = None
        self.live.fetch.return_value = {"synthetic": True}
        code, result, _, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["counts"]["fetched"], 1)
        self.assertEqual(result["data"]["counts"]["skipped"], 3)
        self.assertEqual(self.record("summary", "2026-01-01")["attempts"], 2)

    def test_auth_and_rate_limit_stop_immediately_and_persist_error(self):
        for error, expected in [(BackendError("auth_required", "Authentication required.", 401), 3),
                                (BackendError("rate_limited", "Rate limited.", 429), 5)]:
            with self.subTest(code=error.code):
                if self.db.exists():
                    self.db.unlink()
                self.live.fetch.reset_mock()
                self.live.fetch.side_effect = [error, {}]
                code, result, _, _ = self.sync()
                self.assertEqual(code, expected)
                self.assertEqual(self.live.fetch.call_count, 1)
                self.assertEqual(result["data"]["counts"]["missing"], 3)
                self.assertEqual(self.record("summary", "2026-01-01")["status"], "error")
                self.assertFalse(result["ok"])

    def test_interruption_persists_completed_request_and_resumes(self):
        self.live.fetch.side_effect = [{"synthetic": 1}, KeyboardInterrupt()]
        code, result, _, _ = self.sync()
        self.assertEqual(code, 130)
        self.assertTrue(result["data"]["interrupted"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["data"]["counts"]["fetched"], 1)
        self.assertEqual(result["data"]["counts"]["missing"], 3)
        self.assertEqual(self.record("summary", "2026-01-01")["status"], "available")
        self.live.fetch.reset_mock()
        self.live.fetch.side_effect = None
        self.live.fetch.return_value = {}
        code, result, _, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["counts"]["skipped"], 1)
        self.assertEqual(result["data"]["counts"]["fetched"], 3)

    def test_30_day_default_grid_interrupted_resumed_and_skipped(self):
        args = ("sync", "--from", "2026-09-14", "--to", "2026-10-13", "--delay", "0")
        self.live.fetch.side_effect = [{}] * 8 + [KeyboardInterrupt()]
        code, result, _, _ = self.invoke(*args)
        self.assertEqual(code, 130)
        self.assertEqual(result["data"]["counts"]["requested"], 450)
        self.assertEqual(result["data"]["counts"]["fetched"], 8)
        self.assertEqual(result["data"]["counts"]["missing"], 442)
        self.assertEqual(result["data"]["metrics"], list(METRICS))
        self.live.fetch.side_effect = None
        self.live.fetch.return_value = {"synthetic": 0}
        code, result, _, _ = self.invoke(*args)
        self.assertEqual(code, 0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["data"]["counts"]["fetched"], 442)
        self.assertEqual(result["data"]["counts"]["skipped"], 8)
        self.live.fetch.reset_mock()
        code, result, _, _ = self.invoke(*args)
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["counts"]["skipped"], 450)
        self.live.fetch.assert_not_called()

    def test_offline_no_backend_and_missing_error_empty_are_distinct(self):
        self.save("summary", "2026-01-01", data={})
        self.save("summary", "2026-01-02", error={"code": "not_found", "message": "Absent.", "status": 404})
        code, result, _, constructor = self.invoke(
            "--gccli", "/nonexistent/gccli", "health", "summary",
            "--from", "2026-01-01", "--to", "2026-01-03", "--offline")
        self.assertEqual(code, 1)
        self.assertEqual([r["status"] for r in result["data"]["records"]], ["empty", "error", "missing"])
        constructor.assert_not_called()
        self.assertFalse(result["complete"])

    def test_snapshot_order_and_explicit_account_selection(self):
        for metric in ["summary", "sleep", "hrv", "training-readiness"]:
            self.save(metric, "2026-01-01", data={"metric": metric})
        code, result, _, constructor = self.invoke("snapshot", "--date", "2026-01-01", "--offline")
        self.assertEqual(code, 0)
        self.assertEqual([r["metric"] for r in result["data"]["records"]],
                         ["summary", "sleep", "hrv", "training-readiness"])
        constructor.assert_not_called()
        self.save("summary", "2026-01-01", data={}, account=OTHER_ACCOUNT)
        code, result, _, _ = self.invoke("snapshot", "--date", "2026-01-01")
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"][0]["code"], "account_required")
        code, result, _, _ = self.invoke("--account-id", ACCOUNT, "snapshot", "--date", "2026-01-01")
        self.assertEqual(code, 0)

    def test_empty_cache_explicit_account_returns_missing_grid(self):
        code, result, _, constructor = self.invoke("--account-id", ACCOUNT, "health", "steps",
                                                  "--from", "2026-01-01", "--to", "2026-01-02")
        self.assertEqual(code, 1)
        self.assertEqual(len(result["data"]["records"]), 2)
        self.assertTrue(all(r["status"] == "missing" for r in result["data"]["records"]))
        constructor.assert_not_called()

    def test_export_exact_envelope_and_no_overwrite(self):
        self.save("summary", "2026-01-01", data={"zero": 0})
        export = self.root / "export.json"
        args = ("health", "summary", "--from", "2026-01-01", "--to", "2026-01-01", "--out", str(export))
        code, result, _, _ = self.invoke(*args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(export.read_text()), result)
        original = export.read_bytes()
        code, result, _, _ = self.invoke(*args)
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"][0]["code"], "destination_exists")
        self.assertEqual(export.read_bytes(), original)

    def test_incomplete_export_is_explicit_and_nonzero(self):
        export = self.root / "incomplete.json"
        code, result, _, _ = self.invoke("--account-id", ACCOUNT, "health", "summary",
                                         "--from", "2026-01-01", "--to", "2026-01-01", "--out", str(export))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertFalse(json.loads(export.read_text())["complete"])

    def test_invalid_values_fail_before_backend_or_database(self):
        cases = [
            ["sync", "--from", "2026-01-02", "--to", "2026-01-01"],
            ["sync", "--from", "2026-1-01", "--to", "2026-01-01"],
            ["sync", "--from", "2026-01-01", "--to", "2026-01-01", "--metrics", "summary,unknown"],
            ["sync", "--from", "2026-01-01", "--to", "2026-01-01", "--delay", "nan"],
            ["sync", "--from", "2026-01-01", "--to", "2026-01-01", "--delay", "-1"],
            ["--timeout", "inf", "doctor"],
            ["--timeout", "0", "doctor"],
            ["health", "unknown", "--from", "2026-01-01", "--to", "2026-01-01"],
            ["activities", "list", "--limit", "1001"],
            ["activities", "list", "--limit", "0"],
            ["activities", "download", "-2", "--format", "fit", "--out", str(self.root / "activity.fit")],
            ["activities", "download", "2", "--format", "exe", "--out", str(self.root / "activity.exe")],
            ["unknown-command"], [],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, result, _, constructor = self.invoke(*arguments)
                self.assertEqual(code, 2)
                self.assertFalse(result["ok"])
                constructor.assert_not_called()
                self.assertFalse(self.db.exists())

    def test_doctor_devices_activities_and_download_envelopes(self):
        code, result, _, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["account_id"], ACCOUNT)
        self.assertTrue(result["data"]["history"]["ready"])
        self.live.auth_status.assert_called_once_with()
        code, result, _, _ = self.invoke("devices", "list")
        self.assertEqual(result["data"][0]["deviceId"], 123)
        self.assertEqual(code, 0)
        code, result, _, _ = self.invoke("activities", "list", "--limit", "12")
        self.assertEqual(code, 0)
        self.live.activities.assert_called_once_with(12)
        target = self.root / "synthetic.fit"
        self.live.download.return_value = {"path": str(target), "bytes": 5, "activity_id": "456", "format": "fit"}
        code, result, _, _ = self.invoke("activities", "download", "456", "--format", "fit", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(result["command"], "activities")
        self.live.download.assert_called_once_with("456", "fit", target)

    def test_backend_errors_and_local_database_failures_have_envelopes(self):
        self.live.devices.side_effect = BackendError("executable_missing", "gccli is missing.")
        code, result, _, _ = self.invoke("devices", "list")
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"][0]["code"], "executable_missing")
        self.db.write_text("Unrelated file must remain unchanged")
        code, result, _, _ = self.invoke("health", "summary", "--from", "2026-01-01", "--to", "2026-01-01")
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"][0]["code"], "local_error")
        self.assertEqual(self.db.read_text(), "Unrelated file must remain unchanged")

    def test_doctor_retains_version_and_history_diagnostics_on_expired_auth(self):
        self.live.account_id.side_effect = BackendError("auth_expired", "Authentication required.")
        self.live.auth_status.return_value = {"authenticated": False, "expired": True, "domain": "garmin.com", "account_id": ACCOUNT}
        code, result, _, _ = self.invoke("doctor")
        self.assertEqual(code, 3)
        self.assertEqual(result["data"]["gccli"]["version"], "gccli 1.13.0")
        self.assertFalse(result["data"]["authenticated"])
        self.assertTrue(result["data"]["expired"])
        self.assertTrue(result["data"]["history"]["ready"])

    def test_doctor_saved_expired_auth_can_refresh_on_live_request(self):
        self.live.auth_status.return_value = {"authenticated": False, "expired": True, "domain": "garmin.com", "account_id": ACCOUNT}
        code, result, _, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertTrue(result["data"]["credentials_available"])
        self.assertFalse(result["data"]["authenticated"])
        self.assertTrue(result["data"]["expired"])

    def test_auth_and_rate_codes_stop_without_http_status(self):
        for code_name, expected in [("auth_expired", 3), ("auth_error", 3),
                                    ("unauthorized", 3), ("forbidden", 3), ("too_many_requests", 5)]:
            with self.subTest(code_name=code_name):
                if self.db.exists():
                    self.db.unlink()
                self.live.fetch.reset_mock()
                self.live.fetch.side_effect = BackendError(code_name, "Synthetic failure.")
                code, result, _, _ = self.sync()
                self.assertEqual(code, expected)
                self.assertEqual(self.live.fetch.call_count, 1)
                self.assertEqual(result["data"]["counts"]["missing"], 3)

    def test_sync_rejects_mismatched_live_account(self):
        code, result, _, _ = self.invoke("--account-id", OTHER_ACCOUNT, "sync",
                                         "--from", "2026-01-01", "--to", "2026-01-01", "--delay", "0")
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"][0]["code"], "account_mismatch")
        self.live.fetch.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_module_entrypoint_and_missing_binary(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run([sys.executable, "-m", "garmcli", "--help"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("garmcli", result.stdout)
        result = subprocess.run([sys.executable, "-m", "garmcli", "--json", "--gccli",
                                 str(self.root / "missing"), "devices", "list"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        response = json.loads(result.stdout)
        self.assertFalse(response["ok"])
        self.assertEqual(response["errors"][0]["code"], "executable_missing")


if __name__ == "__main__":
    unittest.main()
