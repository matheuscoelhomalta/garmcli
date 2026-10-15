"""Native Garmin sessions, with tokens confined to the system credential store."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .errors import BackendError


DOMAINS = frozenset({"garmin.com", "garmin.cn"})
SERVICE = "garmcli"


def data_directory():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "garmcli"


def account_identity(email, domain="garmin.com"):
    if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email.strip()):
        raise BackendError("invalid_input", "A valid Garmin account email is required.")
    if not isinstance(domain, str) or domain.strip().casefold() not in DOMAINS:
        raise BackendError("invalid_input", "Domain must be garmin.com or garmin.cn.")
    email, domain = email.strip().casefold(), domain.strip().casefold()
    account_id = hashlib.sha256(f"{domain}:{email}".encode("utf-8")).hexdigest()
    return account_id, email, domain


def _valid_id(account_id):
    return isinstance(account_id, str) and re.fullmatch(r"[0-9a-f]{64}", account_id) is not None


def _json(value):
    def reject_constant(_value):
        raise ValueError("Non-finite JSON number")
    return json.loads(value, parse_constant=reject_constant)


def _validated_record(record):
    try:
        if not isinstance(record, dict):
            raise ValueError()
        account_id, email, domain = account_identity(record.get("email"), record.get("domain"))
        if record.get("account_id") != account_id:
            raise ValueError()
        display_name = record.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError()
        tokens = record.get("tokens")
        if not isinstance(tokens, str):
            raise ValueError()
        payload = _json(tokens)
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), str) or not payload[key].strip()
            for key in ("di_token", "di_refresh_token", "di_client_id")
        ):
            raise ValueError()
        # The pinned SDK persists only DI credentials. Do not retain arbitrary
        # imported fields or a password in a garmcli credential envelope.
        tokens = json.dumps({key: payload[key] for key in ("di_token", "di_refresh_token", "di_client_id")})
        return {"account_id": account_id, "email": email, "domain": domain,
                "display_name": display_name, "tokens": tokens}
    except (ValueError, TypeError, BackendError):
        raise BackendError("auth_error", "The stored Garmin session is invalid; log in again.") from None


def _store_failure(write=False):
    if write:
        return BackendError("auth_storage_error", "The Garmin session could not be saved in the system credential store.")
    return BackendError("credential_store_unavailable", "The system credential store is unavailable or locked.")


def _native_keyring():
    # Select a known OS provider directly. Global keyring configuration and
    # plugin discovery may otherwise select a plaintext or null backend.
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        else:
            raise RuntimeError()
        backend = Keyring()
        if backend.priority <= 0:
            raise RuntimeError()
        return backend
    except Exception:
        raise _store_failure() from None


class SessionStore:
    def __init__(self, data_dir=None):
        self.directory = Path(data_dir) if data_dir is not None else data_directory()
        self.config_path = self.directory / "accounts.json"
        self._backend = None

    def _keyring(self):
        if self._backend is None:
            self._backend = _native_keyring()
        return self._backend

    def _config(self):
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.config_path, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError()
                config = _json(stream.read(1024 * 1024))
            if not isinstance(config, dict) or set(config) != {"default", "accounts"}:
                raise ValueError()
            accounts = config["accounts"]
            if not isinstance(accounts, list) or any(not _valid_id(value) for value in accounts):
                raise ValueError()
            if len(set(accounts)) != len(accounts):
                raise ValueError()
            if config["default"] is not None and config["default"] not in accounts:
                raise ValueError()
            return config
        except FileNotFoundError:
            return {"default": None, "accounts": []}
        except (OSError, ValueError, TypeError):
            raise BackendError("filesystem_error", "The local account configuration could not be read safely.") from None

    def _private_directory(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise OSError()
        self.directory.chmod(0o700)

    @contextmanager
    def _file_lock(self, name):
        fd = None
        try:
            import fcntl
            self._private_directory()
            fd = os.open(self.directory / name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise OSError()
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (OSError, ImportError):
            if fd is not None:
                os.close(fd)
            raise BackendError("filesystem_error", "The local account lock could not be acquired safely.") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def lock(self, account_id):
        if not _valid_id(account_id):
            raise BackendError("invalid_input", "A valid account ID is required.")
        with self._file_lock(f"session-{account_id}.lock"):
            yield

    def _write_config(self, config):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".accounts-", dir=self.directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
        except OSError:
            raise BackendError("filesystem_error", "The local account configuration could not be saved.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def load(self, account_id=None):
        config = self._config()
        account_id = config["default"] if account_id is None else account_id
        if account_id is not None and not _valid_id(account_id):
            raise BackendError("invalid_input", "A valid account ID is required.")
        if account_id is None or account_id not in config["accounts"]:
            raise BackendError("auth_required", "Log in to Garmin with garmcli auth login.")
        try:
            serialized = self._keyring().get_password(SERVICE, account_id)
        except Exception:
            raise _store_failure() from None
        if serialized is None:
            raise BackendError("auth_required", "Log in to Garmin with garmcli auth login.")
        try:
            record = _validated_record(_json(serialized))
        except (ValueError, TypeError):
            raise BackendError("auth_error", "The stored Garmin session is invalid; log in again.") from None
        if record["account_id"] != account_id:
            raise BackendError("auth_error", "The stored Garmin session does not match the selected account.")
        return record

    def save(self, record, select=True):
        record = _validated_record(record)
        with self._file_lock("accounts.lock"):
            config = self._config()
            backend = self._keyring()
            try:
                backend.set_password(SERVICE, record["account_id"], json.dumps(record))
            except Exception:
                raise _store_failure(write=True) from None
            updated = {"default": record["account_id"] if select else config["default"],
                       "accounts": list(config["accounts"])}
            if record["account_id"] not in updated["accounts"]:
                updated["accounts"].append(record["account_id"])
            if updated != config:
                self._write_config(updated)

    def delete(self, account_id=None):
        config = self._config()
        account_id = config["default"] if account_id is None else account_id
        if account_id is None:
            return self.status()
        if not _valid_id(account_id):
            raise BackendError("invalid_input", "A valid account ID is required.")
        with self.lock(account_id), self._file_lock("accounts.lock"):
            config = self._config()
            backend = self._keyring()
            try:
                if backend.get_password(SERVICE, account_id) is not None:
                    backend.delete_password(SERVICE, account_id)
            except Exception:
                raise _store_failure(write=True) from None
            updated = {"default": None if config["default"] == account_id else config["default"],
                       "accounts": [value for value in config["accounts"] if value != account_id]}
            if updated != config:
                self._write_config(updated)
        return self.status()

    def status(self):
        config = self._config()
        result = {"authenticated": False, "expired": False, "credentials_available": False,
                  "domain": None, "account_id": config["default"]}
        if config["default"] is None:
            return result
        try:
            record = self.load(config["default"])
        except BackendError as error:
            if error.code == "auth_required":
                return result
            raise
        result.update(authenticated=True, credentials_available=True, domain=record["domain"])
        try:
            token = _json(record["tokens"])["di_token"]
            encoded = token.split(".")[1]
            payload = _json(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if not isinstance(payload, dict):
                return result
            expiry = payload.get("exp")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                return result
            expires_at = datetime.fromtimestamp(expiry, timezone.utc)
            result["expires_at"] = expires_at.isoformat()
            result["expired"] = expires_at.timestamp() <= datetime.now(timezone.utc).timestamp() + 60
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError):
            pass
        return result


def _sdk():
    logging.getLogger("garminconnect")
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if (name == "garminconnect" or name.startswith("garminconnect.")) and isinstance(logger, logging.Logger):
            logger.handlers[:] = [logging.NullHandler()]
            logger.propagate = False
    try:
        from garminconnect import Garmin
        return Garmin
    except ImportError:
        raise BackendError("dependency_missing", "Install garmcli with its Garmin SDK dependencies.") from None


def _validate_timeout(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise BackendError("invalid_input", "Timeout must be a positive finite number.")


def create_client(record, timeout=120, store=None):
    _validate_timeout(timeout)
    source_record = record
    record = _validated_record(record)
    try:
        api = _sdk()(is_cn=record["domain"] == "garmin.cn", retry_attempts=0)
        api.client.loads(record["tokens"])
        api.display_name = record["display_name"]
        if store is not None:
            original_refresh = api.client._refresh_session

            def refresh_session():
                original_refresh()
                # Persist before the SDK starts/replays an API request. Saving
                # inside _refresh_di_token would let the SDK swallow failures.
                persist_client(api, source_record, store)

            api.client._refresh_session = refresh_session
        return api
    except BackendError:
        raise
    except Exception:
        raise BackendError("auth_error", "The Garmin session could not be restored; log in again.") from None


def persist_client(client, record, store):
    try:
        updated = dict(record, tokens=client.client.dumps())
        updated = _validated_record(updated)
    except BackendError:
        raise
    except Exception:
        raise BackendError("auth_error", "The Garmin session could not be saved.") from None
    if _json(updated["tokens"]) != _json(record["tokens"]):
        store.save(updated, select=False)
        record["tokens"] = updated["tokens"]


def login(email, password, domain="garmin.com", prompt_mfa=None, timeout=120, store=None):
    _validate_timeout(timeout)
    account_id, email, domain = account_identity(email, domain)
    if not isinstance(password, str) or not password:
        raise BackendError("invalid_input", "A Garmin password is required.")
    store = SessionStore() if store is None else store
    with store.lock(account_id):
        try:
            api = _sdk()(is_cn=domain == "garmin.cn", retry_attempts=0)
            api.client.login(email, password, prompt_mfa=prompt_mfa)
            profile = api.client.connectapi("/userprofile-service/socialProfile", timeout=timeout)
            display_name = profile.get("displayName") if isinstance(profile, dict) else None
            record = _validated_record({"account_id": account_id, "email": email, "domain": domain,
                                        "display_name": display_name, "tokens": api.client.dumps()})
            store.save(record)
        except BackendError:
            raise
        except Exception as error:
            name = type(error).__name__
            status = getattr(getattr(error, "response", None), "status_code", None)
            if name == "GarminConnectTooManyRequestsError" or status == 429:
                raise BackendError("rate_limited", "Garmin rate limit reached.", 429) from None
            if name == "GarminConnectAuthenticationError" or status == 401:
                raise BackendError("auth_required", "Garmin authentication failed; check login details and MFA.", 401) from None
            if "Timeout" in name:
                raise BackendError("timeout", "Garmin authentication timed out.") from None
            if status == 403:
                raise BackendError("forbidden", "Garmin authentication was refused.", 403) from None
            raise BackendError("backend_error", "Garmin authentication could not be completed.") from None
    return store.status()
