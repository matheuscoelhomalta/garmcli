import base64
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from garminconnect import Garmin
import requests

from garmcli.backend import BackendError, GarminBackend
from garmcli.contracts import METRICS
from garmcli import worker


ACCOUNT = "a" * 64
RECORD = {"account_id": ACCOUNT, "email": "synthetic@example.com", "domain": "garmin.com",
          "display_name": "synthetic user", "tokens": '{"di_token":"synthetic-token"}'}


def response(data=None, *, status=200, content=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode() if content is None else content
    result._content_consumed = True
    result.headers.update(headers or {})
    result.url = "https://connectapi.garmin.com/synthetic"
    return result


def zip_contents(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


class NativeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.api = Garmin()
        self.api.client.loads(RECORD["tokens"])
        self.session = Mock()
        self.api.client._api_session = self.session
        self.store = Mock()
        self.store.lock.return_value = nullcontext()
        self.store.load.return_value = dict(RECORD)
        self.patches = [
            patch("garmcli.auth.SessionStore", return_value=self.store),
            patch("garmcli.auth.create_client", return_value=self.api),
            patch("garmcli.auth.persist_client"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def execute(self, operation="health", **kwargs):
        return worker.execute({"operation": operation, "account_id": ACCOUNT, "timeout": 120, **kwargs})

    def test_all_fifteen_exact_raw_gets_use_real_pinned_sdk(self):
        expected = {
            "summary": ("/usersummary-service/usersummary/daily/synthetic%20user", {"calendarDate": "2026-01-01"}),
            "steps": ("/wellness-service/wellness/dailySummaryChart/synthetic%20user", {"date": "2026-01-01"}),
            "heart-rate": ("/wellness-service/wellness/dailyHeartRate/synthetic%20user", {"date": "2026-01-01"}),
            "floors": ("/wellness-service/wellness/floorsChartData/daily/2026-01-01", {}),
            "sleep": ("/wellness-service/wellness/dailySleepData/synthetic%20user", {"date": "2026-01-01", "nonSleepBufferMinutes": 60}),
            "stress": ("/wellness-service/wellness/dailyStress/2026-01-01", {}),
            "respiration": ("/wellness-service/wellness/daily/respiration/2026-01-01", {}),
            "spo2": ("/wellness-service/wellness/daily/spo2/2026-01-01", {}),
            "hrv": ("/hrv-service/hrv/2026-01-01", {}),
            "body-battery": ("/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": "2026-01-01", "endDate": "2026-01-01"}),
            "intensity-minutes": ("/wellness-service/wellness/daily/im/2026-01-01", {}),
            "training-readiness": ("/metrics-service/metrics/trainingreadiness/2026-01-01", {}),
            "training-status": ("/metrics-service/metrics/trainingstatus/aggregated/2026-01-01", {}),
            "fitness-age": ("/fitnessage-service/fitnessage/2026-01-01", {}),
            "max-metrics": ("/metrics-service/metrics/maxmet/daily/2026-01-01/2026-01-01", {}),
        }
        self.assertEqual(set(expected), set(METRICS))
        payload = {"zero": 0, "false": False, "sentinel": -1, "privacyProtected": True,
                   "lastSevenDaysAvgSpO2": "97.5", "nested": [None, {"date": "2025-12-31"}]}
        for metric, (path, params) in expected.items():
            with self.subTest(metric=metric):
                self.session.request.return_value = response(payload)
                self.assertEqual(self.execute(metric=metric, date="2026-01-01"), payload)
                args, kwargs = self.session.request.call_args
                self.assertEqual(args, ("GET", "https://connectapi.garmin.com" + path))
                self.assertEqual(kwargs["params"], params)
                self.assertNotIn("?", args[1])
                self.assertLessEqual(kwargs["timeout"], 120)
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-token")
                self.assertEqual(kwargs["headers"]["Accept"], "application/json")
                # Each execute constructs one client, so restore its original transport.
                self.api.client._api_session = self.session
        self.store.load.assert_called_with(ACCOUNT)
        self.store.lock.assert_called_with(ACCOUNT)

    def test_payload_absence_primitives_and_nested_values_are_not_transformed(self):
        for payload in (None, {}, [], 0, False, "", {"values": []}, [0, -1, None]):
            with self.subTest(payload=payload):
                self.session.request.return_value = response(payload)
                self.assertEqual(self.execute(metric="summary", date="2026-01-01"), payload)
                self.api.client._api_session = self.session
        for operation, expected in (("health", {}), ("devices", {}), ("activities", None)):
            for status in (200, 204):
                self.session.request.return_value = response(status=status, content=b"")
                args = {"metric": "summary", "date": "2026-01-01"} if operation == "health" else {"limit": 1} if operation == "activities" else {}
                self.assertEqual(self.execute(operation, **args), expected)
                self.api.client._api_session = self.session

    def test_devices_and_activity_limits_use_raw_native_requests(self):
        self.session.request.return_value = response([{"deviceId": 0}])
        self.assertEqual(self.execute("devices"), [{"deviceId": 0}])
        self.assertEqual(self.session.request.call_args.args[1], "https://connectapi.garmin.com/device-service/deviceregistration/devices")
        self.api.client._api_session = self.session
        self.session.request.return_value = response(None)
        self.assertIsNone(self.execute("activities", limit=1000))
        self.assertEqual(self.session.request.call_args.kwargs["params"], {"start": 0, "limit": 1000})

    def test_native_401_refresh_replays_and_persists(self):
        self.session.request.side_effect = [response(status=401), response({"recovered": True})]
        def refresh():
            self.api.client.di_token = "synthetic-rotated"
        self.api.client._refresh_session = Mock(side_effect=refresh)
        self.assertEqual(self.execute("devices"), {"recovered": True})
        self.api.client._refresh_session.assert_called_once_with()
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].kwargs["headers"]["Authorization"], "Bearer synthetic-token")
        self.assertEqual(calls[1].kwargs["headers"]["Authorization"], "Bearer synthetic-rotated")
        self.patches[-1].target.persist_client.assert_called_once_with(self.api, self.store.load.return_value, self.store)

    def test_error_status_and_sensitive_messages_are_sanitized(self):
        for status, code in ((401, "auth_required"), (403, "error"), (404, "not_found"), (429, "rate_limited"), (503, "error")):
            self.session.request.side_effect = None
            self.session.request.return_value = response({"message": "SECRET synthetic@example.com <html>body</html>"}, status=status,
                                                         headers={"Retry-After": "31"})
            with patch("garmcli.worker.time.sleep"), self.assertRaises(Exception) as raised:
                self.execute("devices")
            error = worker.error_details(raised.exception)
            self.assertEqual(error["code"], code)
            self.assertEqual(error["status"], status)
            self.assertNotIn("SECRET", json.dumps(error))
            self.assertNotIn("example.com", json.dumps(error))
            self.api.client._api_session = self.session

    def test_invalid_json_and_nonfinite_values_fail_safely(self):
        for content in (b"secret not json", b'{"value":NaN}', b'{"value":Infinity}', b"\xff"):
            self.session.request.return_value = response(content=content)
            with self.assertRaises(BackendError) as raised:
                self.execute("devices")
            self.assertEqual(raised.exception.code, "invalid_json")
            self.assertNotIn("secret", raised.exception.message)
            self.api.client._api_session = self.session

    def test_all_download_formats_fit_zip_and_csv_splits(self):
        for fmt in ("fit", "gpx", "tcx", "kml", "csv"):
            contents = b"split,distance\n1,1000\n" if fmt == "csv" else b"synthetic bytes"
            wire = zip_contents([("ignored.txt", b"text"), ("nested/first.FIT", contents), ("second.fit", b"wrong")]) if fmt == "fit" else contents
            def negotiate_download(method, url, **options):
                if options["headers"].get("Accept") != "*/*":
                    return response({"message": "JSON is unavailable for this download"}, status=406)
                return response(content=wire)
            self.session.request.side_effect = negotiate_download
            result = self.execute("download", activity_id="123", format=fmt)
            self.assertEqual(base64.b64decode(result["content"]), contents)
            suffix = "/download-service/files/activity/123" if fmt == "fit" else f"/download-service/export/{fmt}/activity/123"
            self.assertTrue(self.session.request.call_args.args[1].endswith(suffix))
            self.assertEqual(self.session.request.call_args.kwargs["headers"]["Accept"], "*/*")
            self.api.client._api_session = self.session
        self.session.request.side_effect = None
        for wire in (b"not zip", zip_contents([("other.txt", b"none")]), zip_contents([("empty.fit", b"")])):
            self.session.request.return_value = response(content=wire)
            with self.assertRaises(BackendError) as raised:
                self.execute("download", activity_id="123", format="fit")
            self.assertEqual(raised.exception.code, "download_failed")
            self.api.client._api_session = self.session

    def test_invalid_worker_inputs_never_lookup_credentials_or_make_requests(self):
        invalid = [{"operation": "delete"}, {"operation": "health", "metric": "unknown", "date": "2026-01-01"},
                   {"operation": "health", "metric": "summary", "date": "today"},
                   {"operation": "activities", "limit": True}, {"operation": "activities", "limit": 1001},
                   {"operation": "download", "activity_id": "1/../2", "format": "fit"},
                   {"operation": "download", "activity_id": "1", "format": "exe"}]
        for values in invalid:
            with self.assertRaises(BackendError):
                worker.execute({"account_id": ACCOUNT, "timeout": 120, **values})
        self.store.load.assert_not_called()
        self.session.request.assert_not_called()


class RetryTests(unittest.TestCase):
    def test_429_honors_header_with_three_retries_and_5xx_two_retries(self):
        session = Mock()
        session.request.side_effect = [response(status=429, headers={"Retry-After": "2"}) for _ in range(4)]
        with patch("garmcli.worker.time.sleep") as sleep:
            result = worker.RetrySession(session, time.monotonic() + 120).request("GET", "synthetic")
        self.assertEqual(result.status_code, 429)
        self.assertEqual(session.request.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 2, 2])
        session.reset_mock()
        session.request.side_effect = [response(status=503) for _ in range(3)]
        with patch("garmcli.worker.time.sleep") as sleep, patch("garmcli.worker.random.random", return_value=0):
            result = worker.RetrySession(session, time.monotonic() + 120).request("GET", "synthetic")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    def test_long_rate_wait_client_errors_and_network_failures_are_not_retried(self):
        for status in (401, 403, 404, 429):
            session = Mock()
            session.request.return_value = response(status=status, headers={"Retry-After": "31"})
            worker.RetrySession(session, time.monotonic() + 120).request("GET", "synthetic")
            self.assertEqual(session.request.call_count, 1)
        for error in (requests.Timeout("SECRET"), requests.ConnectionError("SECRET")):
            session = Mock()
            session.request.side_effect = error
            with self.assertRaises(type(error)):
                worker.RetrySession(session, time.monotonic() + 120).request("GET", "synthetic")
            self.assertEqual(session.request.call_count, 1)
            self.assertNotIn("SECRET", json.dumps(worker.error_details(error)))

    def test_retry_after_dates_defaults_and_deadline(self):
        for value in (None, "", "nonsense", "-2", "0"):
            self.assertEqual(worker.retry_after(value), 1)
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
        self.assertGreater(worker.retry_after(future), 18)
        session = Mock()
        session.request.return_value = response(status=429, headers={"Retry-After": "5"})
        with patch("garmcli.worker.time.sleep") as sleep, self.assertRaises(BackendError) as raised:
            worker.RetrySession(session, time.monotonic() + 0.2).request("GET", "synthetic")
        self.assertEqual(raised.exception.code, "timeout")
        sleep.assert_not_called()
        self.assertLess(session.request.call_args.kwargs["timeout"], 0.2)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = GarminBackend(timeout=1)
        self.backend._auth = {"account_id": ACCOUNT, "authenticated": True}

    def test_validations_precede_worker_and_filesystem_changes(self):
        with patch("garmcli.backend.subprocess.Popen") as start:
            for operation in [lambda: self.backend.fetch("delete", "2026-01-01"), lambda: self.backend.fetch([], "2026-01-01"),
                              lambda: self.backend.fetch("summary", "today"),
                              *[lambda value=value: self.backend.activities(value) for value in (0, -1, 1001, True, "2")],
                              lambda: self.backend.download("000", "fit", self.directory / "x"),
                              lambda: self.backend.download("1", "exe", self.directory / "x")]:
                with self.assertRaises(BackendError):
                    operation()
            for value in (0, -1, True, float("inf"), float("nan"), "2"):
                with self.assertRaises(BackendError):
                    GarminBackend(timeout=value)
            start.assert_not_called()

    def test_identity_is_resolved_once_and_pinned_without_token_ipc(self):
        store = Mock()
        store.status.return_value = {"account_id": ACCOUNT, "authenticated": True}
        backend = GarminBackend()
        with patch("garmcli.auth.SessionStore", return_value=store), patch("garmcli.backend.subprocess.Popen") as start:
            process = start.return_value
            process.communicate.return_value = (b'{"ok":true,"data":{}}', None)
            process.returncode = 0
            process.poll.return_value = 0
            self.assertEqual(backend.account_id(), ACCOUNT)
            store.status.return_value = {"account_id": "b" * 64}
            backend.devices()
            backend.activities(1)
            store.status.assert_called_once_with()
            request = json.loads(process.communicate.call_args_list[0].args[0])
            self.assertEqual(request["account_id"], ACCOUNT)
            self.assertNotIn("tokens", request)
            self.assertEqual(start.call_args.args[0], [sys.executable, "-m", "garmcli.worker"])

    def test_download_exclusive_publish_private_mode_cleanup_and_race(self):
        destination = self.directory / "activity.fit"
        contents = b"synthetic activity"
        with patch.object(self.backend, "_request", return_value={"content": base64.b64encode(contents).decode()}) as request:
            result = self.backend.download("123", "fit", destination)
            self.assertEqual(destination.read_bytes(), contents)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(result["bytes"], len(contents))
            with self.assertRaises(BackendError):
                self.backend.download("123", "fit", destination)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(list(self.directory.glob(".garmcli-*")), [])
        destination.unlink()
        def racing(*args, **kwargs):
            destination.write_bytes(b"other owner")
            return {"content": base64.b64encode(contents).decode()}
        with patch.object(self.backend, "_request", side_effect=racing), self.assertRaises(BackendError):
            self.backend.download("123", "fit", destination)
        self.assertEqual(destination.read_bytes(), b"other owner")
        self.assertEqual(list(self.directory.glob(".garmcli-*")), [])
        for payload in ({}, {"content": "invalid base64"}, {"content": ""}, None):
            with patch.object(self.backend, "_request", return_value=payload), self.assertRaises(BackendError) as raised:
                self.backend.download("1", "fit", self.directory / "missing.fit")
            self.assertEqual(raised.exception.code, "download_failed")
            self.assertFalse((self.directory / "missing.fit").exists())

    def test_child_failures_and_protocol_are_sanitized(self):
        for payload in (b"SECRET", b'{"ok":true,"data":NaN}', b'[]'):
            with patch("garmcli.backend.subprocess.Popen") as start:
                process = start.return_value
                process.communicate.return_value = (payload, None)
                process.poll.return_value = 0
                process.returncode = 0
                with self.assertRaises(BackendError) as raised:
                    self.backend.devices()
                self.assertNotIn("SECRET", raised.exception.message)
        with patch("garmcli.backend.subprocess.Popen") as start:
            process = start.return_value
            process.communicate.return_value = (b'{"ok":false,"error":{"code":"not_found","status":404,"message":"SECRET"}}', None)
            process.poll.return_value = 1
            process.returncode = 1
            with self.assertRaises(BackendError) as raised:
                self.backend.devices()
            self.assertEqual(raised.exception.status, 404)
            self.assertNotIn("SECRET", raised.exception.message)

    def test_total_timeout_and_sigint_reap_real_worker_process(self):
        original = subprocess.Popen
        for interruption in ("timeout", "sigint"):
            process_holder = []
            def start(*args, **kwargs):
                process = original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
                process_holder.append(process)
                if interruption == "sigint":
                    timer = threading.Timer(0.05, os.kill, args=(os.getpid(), signal.SIGINT))
                    timer.start()
                    self.addCleanup(timer.cancel)
                return process
            with patch("garmcli.backend.subprocess.Popen", side_effect=start):
                self.backend.timeout = 0.05 if interruption == "timeout" else 1
                expected = BackendError if interruption == "timeout" else KeyboardInterrupt
                started = time.monotonic()
                with self.assertRaises(expected) as raised:
                    self.backend.devices()
                self.assertIsNotNone(process_holder[0].poll())
                self.assertLess(time.monotonic() - started, 2)
                if interruption == "timeout":
                    self.assertEqual(raised.exception.code, "timeout")


    def test_real_child_protocol_rejects_unknown_operation_without_credentials(self):
        with self.assertRaises(BackendError) as raised:
            self.backend._request("delete")
        self.assertEqual(raised.exception.code, "invalid_input")


class WorkerProtocolTests(unittest.TestCase):
    def test_library_output_and_exceptions_never_leak_into_protocol(self):
        source = Mock()
        source.buffer = io.BytesIO(b'{"operation":"synthetic"}')
        stdout, stderr = io.StringIO(), io.StringIO()
        def noisy(_request):
            print("SECRET synthetic@example.com")
            print("Authorization: SECRET", file=sys.stderr)
            raise RuntimeError("SECRET <html>private body</html>")
        with patch("garmcli.worker.execute", side_effect=noisy), patch("sys.stdin", source), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("SECRET", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "backend_error")


if __name__ == "__main__":
    unittest.main()
