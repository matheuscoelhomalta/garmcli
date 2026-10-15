import base64
from contextlib import contextmanager
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from garmcli import auth
from garmcli.errors import BackendError


def fixture_record(email="synthetic@example.com", domain="garmin.com", access="ACCESS"):
    account_id, email, domain = auth.account_identity(email, domain)
    return {"account_id": account_id, "email": email, "domain": domain, "display_name": "synthetic-display",
            "tokens": json.dumps({"di_token": access, "di_refresh_token": "REFRESH", "di_client_id": "CLIENT"})}


class MemoryKeyring:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.fail = False

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        if self.fail:
            raise RuntimeError("SECRET synthetic@example.com")
        self.values[service, username] = password
        self.writes.append((service, username))

    def delete_password(self, service, username):
        del self.values[service, username]


class FakeNative:
    def __init__(self):
        self.tokens = json.loads(fixture_record()["tokens"])
        self.calls = []
        self.mfa_value = None

    def loads(self, tokens):
        self.tokens = json.loads(tokens)

    def dumps(self):
        return json.dumps(self.tokens)

    def _refresh_session(self):
        self.tokens["di_token"] = "ROTATED-ACCESS"
        self.tokens["di_refresh_token"] = "ROTATED-REFRESH"

    def login(self, email, password, prompt_mfa=None):
        self.calls.append((email, password))
        if prompt_mfa:
            self.mfa_value = prompt_mfa()
        return None, None

    def connectapi(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return {"displayName": "synthetic-display"}


class FakeGarmin:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.client = FakeNative()
        self.instances.append(self)


def acquire_lock(directory, account_id, started, acquired):
    started.set()
    with auth.SessionStore(directory).lock(account_id):
        acquired.set()


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "garmcli"
        self.backend = MemoryKeyring()
        self.store = auth.SessionStore(self.directory)
        self.store._backend = self.backend

    def tearDown(self):
        self.temp.cleanup()


class SessionStoreTests(StoreFixture):
    def test_identity_matches_existing_sha_and_domain_is_part_of_identity(self):
        account_id, email, domain = auth.account_identity("  SYNTHETIC@EXAMPLE.COM ", " GARMIN.COM ")
        self.assertEqual(account_id, hashlib.sha256(b"garmin.com:synthetic@example.com").hexdigest())
        self.assertEqual((email, domain), ("synthetic@example.com", "garmin.com"))
        self.assertNotEqual(account_id, auth.account_identity(email, "garmin.cn")[0])
        self.assertEqual(auth.account_identity("stra\u00dfe@example.com")[0], auth.account_identity("STRASSE@example.com")[0])

    def test_save_load_pin_and_config_contains_only_ids(self):
        first = fixture_record()
        second = fixture_record("second@example.com", "garmin.cn")
        self.store.save(first)
        self.store.save(second)
        self.assertEqual(self.store.load(), second)
        self.assertEqual(self.store.load(first["account_id"]), first)
        self.store.save(first, select=False)
        self.assertEqual(self.store.load(), second)
        config = self.store.config_path.read_text()
        self.assertEqual(json.loads(config), {"default": second["account_id"], "accounts": [first["account_id"], second["account_id"]]})
        for forbidden in ("synthetic@example.com", "ACCESS", "REFRESH", "CLIENT", "display_name", "tokens"):
            self.assertNotIn(forbidden, config)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.config_path.stat().st_mode), 0o600)

    def test_logged_out_does_not_import_sdk_or_initialize_keyring(self):
        with patch.object(auth, "_native_keyring", side_effect=AssertionError("keyring used")), patch.object(auth, "_sdk", side_effect=AssertionError("SDK used")):
            status = self.store.status()
            self.assertFalse(status["authenticated"])
            self.assertIsNone(status["account_id"])
            self.assertFalse(self.directory.exists())
            self.assertEqual(self.store.delete(), status)
            with self.assertRaises(BackendError) as caught:
                self.store.load()
        self.assertEqual(caught.exception.code, "auth_required")

    def test_status_is_local_safe_and_expiration_is_informational(self):
        payload = base64.urlsafe_b64encode(b'{"exp":100}').rstrip(b"=").decode()
        record = fixture_record(access="header." + payload + ".signature")
        self.store.save(record)
        with patch.object(auth, "_sdk", side_effect=AssertionError("SDK used")):
            status = self.store.status()
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["credentials_available"])
        self.assertTrue(status["expired"])
        self.assertEqual(status["expires_at"], "1970-01-01T00:01:40+00:00")
        output = json.dumps(status)
        for forbidden in (record["email"], "REFRESH", "CLIENT", "signature", "display_name"):
            self.assertNotIn(forbidden, output)

    def test_invalid_expiration_does_not_break_status(self):
        for expiry in ('"SECRET"', "true", "{}", "1e999"):
            payload = base64.urlsafe_b64encode(('{"exp":' + expiry + '}').encode()).rstrip(b"=").decode()
            self.store.save(fixture_record(access="header." + payload + ".signature"))
            self.assertNotIn("expires_at", self.store.status())

    def test_pinned_identity_and_envelope_cannot_be_swapped(self):
        record = fixture_record()
        self.store.save(record)
        other = fixture_record("other@example.com")
        self.backend.values[auth.SERVICE, record["account_id"]] = json.dumps(other)
        with self.assertRaises(BackendError) as caught:
            self.store.load(record["account_id"])
        self.assertEqual(caught.exception.code, "auth_error")
        self.backend.values[auth.SERVICE, record["account_id"]] = json.dumps(dict(record, domain="garmin.cn"))
        with self.assertRaises(BackendError):
            self.store.load()

    def test_invalid_credentials_do_not_replace_a_valid_session(self):
        record = fixture_record()
        self.store.save(record)
        for token in ({}, {"di_token": "ACCESS", "di_refresh_token": "REFRESH"}, {"di_token": 1, "di_refresh_token": "REFRESH", "di_client_id": "CLIENT"}):
            with self.assertRaises(BackendError):
                self.store.save(dict(record, tokens=json.dumps(token)))
        self.assertEqual(self.store.load(), record)

    def test_store_write_failure_is_safe_and_does_not_select_failed_login(self):
        self.backend.fail = True
        with self.assertRaises(BackendError) as caught:
            self.store.save(fixture_record())
        self.assertEqual(caught.exception.code, "auth_storage_error")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertFalse(self.store.status()["authenticated"])

    def test_keyring_unavailable_is_not_reported_as_logged_out(self):
        self.store.save(fixture_record())
        self.store._backend = None
        with patch.object(auth, "_native_keyring", side_effect=RuntimeError("SECRET")):
            with self.assertRaises(BackendError) as caught:
                self.store.status()
        self.assertEqual(caught.exception.code, "credential_store_unavailable")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_unavailable_keyring_during_save_preserves_unavailable_error(self):
        self.store._backend = None
        with patch.object(auth, "_native_keyring", side_effect=BackendError("credential_store_unavailable", "Unavailable")):
            with self.assertRaises(BackendError) as caught:
                self.store.save(fixture_record())
        self.assertEqual(caught.exception.code, "credential_store_unavailable")

    def test_logout_deletes_only_selected_garmcli_credentials(self):
        record, other = fixture_record(), fixture_record("other@example.com")
        self.store.save(other)
        self.store.save(record)
        self.backend.values["gccli", "gccli:token:synthetic@example.com"] = "UNRELATED"
        history = self.directory / "history.sqlite3"
        history.write_bytes(b"synthetic history")
        self.assertFalse(self.store.delete()["authenticated"])
        self.assertNotIn((auth.SERVICE, record["account_id"]), self.backend.values)
        self.assertIn((auth.SERVICE, other["account_id"]), self.backend.values)
        self.assertEqual(self.backend.values["gccli", "gccli:token:synthetic@example.com"], "UNRELATED")
        self.assertEqual(history.read_bytes(), b"synthetic history")
        self.assertFalse(self.store.delete()["authenticated"])

    def test_private_lock_serializes_two_processes(self):
        context = multiprocessing.get_context("spawn")
        started, acquired = context.Event(), context.Event()
        account_id = fixture_record()["account_id"]
        process = context.Process(target=acquire_lock, args=(str(self.directory), account_id, started, acquired))
        with self.store.lock(account_id):
            process.start()
            self.assertTrue(started.wait(5))
            self.assertFalse(acquired.wait(0.1))
        self.assertTrue(acquired.wait(5))
        process.join(5)
        if process.is_alive():
            process.terminate()
            self.fail("Locking child did not finish")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(stat.S_IMODE((self.directory / f"session-{account_id}.lock").stat().st_mode), 0o600)

    def test_symlink_lock_and_directory_are_refused(self):
        self.directory.mkdir()
        unrelated = Path(self.temp.name) / "unrelated"
        unrelated.write_text("preserve")
        account_id = fixture_record()["account_id"]
        (self.directory / f"session-{account_id}.lock").symlink_to(unrelated)
        with self.assertRaises(BackendError):
            with self.store.lock(account_id):
                self.fail("Symlink lock accepted")
        self.assertEqual(unrelated.read_text(), "preserve")


class ClientTests(StoreFixture):
    def test_restore_uses_native_in_memory_api_and_refresh_saves_before_return(self):
        record = fixture_record(domain="garmin.cn")
        self.store.save(record)
        with patch.object(auth, "_sdk", return_value=FakeGarmin), patch.dict(os.environ, {"GARMINTOKENS": "/must/not/be/read"}):
            api = auth.create_client(record, timeout=2, store=self.store)
        self.assertEqual(api.kwargs, {"is_cn": True, "retry_attempts": 0})
        self.assertEqual(api.display_name, record["display_name"])
        self.assertEqual(json.loads(api.client.dumps()), json.loads(record["tokens"]))
        writes = len(self.backend.writes)
        api.client._refresh_session()
        self.assertEqual(len(self.backend.writes), writes + 1)
        self.assertEqual(json.loads(self.store.load()["tokens"])["di_refresh_token"], "ROTATED-REFRESH")
        auth.persist_client(api, record, self.store)
        self.assertEqual(len(self.backend.writes), writes + 1)

    def test_refresh_storage_failure_propagates_without_raw_error(self):
        record = fixture_record()
        self.store.save(record)
        with patch.object(auth, "_sdk", return_value=FakeGarmin):
            api = auth.create_client(record, store=self.store)
        self.backend.fail = True
        with self.assertRaises(BackendError) as caught:
            api.client._refresh_session()
        self.assertEqual(caught.exception.code, "auth_storage_error")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_persist_unchanged_client_does_not_write_or_change_selection(self):
        first, selected = fixture_record(), fixture_record("selected@example.com")
        self.store.save(first)
        self.store.save(selected)
        with patch.object(auth, "_sdk", return_value=FakeGarmin):
            api = auth.create_client(first)
        writes = len(self.backend.writes)
        auth.persist_client(api, first, self.store)
        self.assertEqual(len(self.backend.writes), writes)
        api.client._refresh_session()
        auth.persist_client(api, first, self.store)
        self.assertEqual(self.store.load()["account_id"], selected["account_id"])
        self.assertEqual(json.loads(first["tokens"])["di_token"], "ROTATED-ACCESS")

    def test_login_uses_fresh_native_flow_and_callback_without_sdk_token_files(self):
        with patch.object(auth, "_sdk", return_value=FakeGarmin), patch.dict(os.environ, {"GARMINTOKENS": "/must/not/be/read"}):
            status = auth.login(" SYNTHETIC@EXAMPLE.COM ", "password with spaces ", domain="garmin.cn", prompt_mfa=lambda: "123456", timeout=4, store=self.store)
        api = FakeGarmin.instances[-1]
        self.assertEqual(api.client.calls[0], ("synthetic@example.com", "password with spaces "))
        self.assertEqual(api.client.mfa_value, "123456")
        self.assertEqual(api.client.calls[1], ("/userprofile-service/socialProfile", {"timeout": 4}))
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["domain"], "garmin.cn")
        self.assertNotIn("password", json.dumps(self.backend.values.get((auth.SERVICE, status["account_id"]))))

    def test_login_rejects_cookie_only_success_and_preserves_previous_selection(self):
        existing = fixture_record("existing@example.com")
        self.store.save(existing)
        class CookieOnlyGarmin(FakeGarmin):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.client.tokens = {"di_token": None, "di_refresh_token": None, "di_client_id": None}
        with patch.object(auth, "_sdk", return_value=CookieOnlyGarmin):
            with self.assertRaises(BackendError) as caught:
                auth.login("new@example.com", "SYNTHETIC", store=self.store)
        self.assertEqual(caught.exception.code, "auth_error")
        self.assertEqual(self.store.load()["account_id"], existing["account_id"])

    def test_login_authentication_and_rate_errors_are_sanitized(self):
        for name, code in (("GarminConnectAuthenticationError", "auth_required"), ("GarminConnectTooManyRequestsError", "rate_limited")):
            error_type = type(name, (Exception,), {})
            with patch.object(auth, "_sdk", return_value=FakeGarmin), patch.object(FakeNative, "login", side_effect=error_type("SECRET synthetic@example.com")):
                with self.assertRaises(BackendError) as caught:
                    auth.login("synthetic@example.com", "SYNTHETIC", store=self.store)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_sdk_logs_are_contained(self):
        fake_module = types.ModuleType("garminconnect")
        fake_module.Garmin = FakeGarmin
        with patch.dict(sys.modules, {"garminconnect": fake_module}):
            self.assertIs(auth._sdk(), FakeGarmin)
        logger = logging.getLogger("garminconnect")
        self.assertFalse(logger.propagate)
        self.assertTrue(all(isinstance(handler, logging.NullHandler) for handler in logger.handlers))
        with self.assertNoLogs(level="WARNING"):
            logging.getLogger("garminconnect.client").warning("SECRET")

    def test_preexisting_sdk_child_handler_is_also_suppressed(self):
        child = logging.getLogger("garminconnect.client")
        handler = logging.StreamHandler()
        child.addHandler(handler)
        try:
            auth._sdk()
            self.assertTrue(all(isinstance(item, logging.NullHandler) for item in child.handlers))
            self.assertFalse(child.propagate)
        finally:
            child.removeHandler(handler)

    def test_pinned_sdk_refresh_saves_before_replaying_unauthorized_request(self):
        record = fixture_record()
        self.store.save(record)
        api = auth.create_client(record, timeout=2, store=self.store)
        calls = []
        class Response:
            def __init__(self, status):
                self.status_code = status
                self.content = b"{}"
            def json(self):
                return {}
        def request(*args, **kwargs):
            calls.append("request")
            if len(calls) == 1:
                return Response(401)
            persisted = json.loads(self.store.load()["tokens"])
            self.assertEqual(persisted["di_refresh_token"], "ROTATED-REFRESH")
            return Response(200)
        def refresh():
            api.client.di_token = "ROTATED-ACCESS"
            api.client.di_refresh_token = "ROTATED-REFRESH"
        with patch.object(api.client, "_refresh_di_token", side_effect=refresh), patch.object(api.client._api_session, "request", side_effect=request):
            response = api.client.request("GET", "connectapi", "/synthetic", timeout=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["request", "request"])


class NativeBackendTests(unittest.TestCase):
    def test_native_provider_is_used_without_default_backend_or_plaintext_fallback(self):
        module = types.ModuleType("keyring.backends.SecretService")
        class SecureProvider:
            priority = 5
        module.Keyring = SecureProvider
        with patch.object(auth.sys, "platform", "linux"), patch.dict(sys.modules, {"keyring.backends.SecretService": module}):
            self.assertIsInstance(auth._native_keyring(), SecureProvider)
        class Unavailable:
            def __init__(self):
                raise RuntimeError("SECRET")
        module.Keyring = Unavailable
        with patch.object(auth.sys, "platform", "linux"), patch.dict(sys.modules, {"keyring.backends.SecretService": module}):
            with self.assertRaises(BackendError) as caught:
                auth._native_keyring()
        self.assertEqual(caught.exception.code, "credential_store_unavailable")
        self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
