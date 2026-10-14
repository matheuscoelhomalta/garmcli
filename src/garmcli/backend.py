"""A fixed read-only allowlist around gccli; gccli owns HTTP retries."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from .contracts import METRICS, parse_dates, safe_error


DOWNLOAD_FORMATS = frozenset({"fit", "gpx", "tcx", "kml", "csv"})
MAX_ACTIVITY_LIMIT = 1000


class BackendError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        if self.status is not None:
            result["status"] = self.status
        return result


class GarminBackend:
    def __init__(self, executable: str = "gccli", timeout: float = 120):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise BackendError("invalid_input", "Timeout must be a positive finite number.")
        self.executable = os.fspath(executable)
        self.timeout = timeout
        self._auth: dict[str, Any] | None = None
        self._email: str | None = None

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.executable, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            raise BackendError("executable_missing", "gccli executable was not found.") from None
        except subprocess.TimeoutExpired:
            raise BackendError("timeout", "gccli request timed out.") from None
        except OSError:
            raise BackendError("backend_error", "gccli could not be started.") from None
        if result.returncode:
            error = self._failure(result)
            raise BackendError(error["code"], error["message"], error.get("status"))
        return result

    @staticmethod
    def _failure(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        # Only structured code/status are accepted; stderr can contain tokens,
        # email addresses, JSON server bodies or HTML and is never echoed.
        for output in (result.stderr, result.stdout):
            for candidate in [output, *reversed(output.splitlines())]:
                try:
                    payload = json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict):
                    error = payload.get("error", payload)
                    if isinstance(error, dict) and ("code" in error or "status" in error):
                        return safe_error(error)
        code = {3: "auth_required", 5: "rate_limited"}.get(result.returncode, "backend_error")
        return safe_error({"code": code})

    def _json(self, argv: list[str]) -> Any:
        result = self._run(argv)
        try:
            def reject_constant(value):
                raise ValueError("Non-JSON numeric constant.")

            return json.loads(result.stdout, parse_constant=reject_constant)
        except (ValueError, TypeError):
            raise BackendError("invalid_json", "gccli returned invalid JSON.") from None

    def version(self) -> str:
        return self._run(["--version"]).stdout.strip()

    def auth_status(self) -> dict[str, Any]:
        if self._auth is None:
            payload = self._json(["auth", "status", "--json"])
            if not isinstance(payload, dict):
                raise BackendError("auth_required", "Garmin authentication is required.")
            email = payload.get("email")
            domain = payload.get("domain")
            if (
                not isinstance(email, str)
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", email.strip())
                or not isinstance(domain, str)
                or not re.fullmatch(r"[a-zA-Z0-9.-]+", domain.strip())
            ):
                raise BackendError("auth_required", "Garmin authentication is required.")
            domain = domain.strip().casefold()
            email = email.strip().casefold()
            identity = f"{domain}:{email}"
            expired = payload.get("expired") is True
            self._auth = {
                # gccli refreshes stored expired credentials on a live request.
                "authenticated": True,
                "expired": expired,
                "domain": domain,
                "account_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            }
            expires_at = payload.get("expires_at")
            if isinstance(expires_at, str):
                # Only an ISO timestamp, never arbitrary auth output, is returned.
                from datetime import datetime

                try:
                    datetime.fromisoformat(expires_at)
                except ValueError:
                    pass
                else:
                    self._auth["expires_at"] = expires_at
            self._email = email
        return dict(self._auth)

    def account_id(self) -> str:
        auth = self.auth_status()
        return auth["account_id"]

    def devices(self) -> Any:
        return self._json(["devices", "list", "--json", *self._account_args()])

    def _account_args(self) -> list[str]:
        # Keep requests pinned to the identity resolved for this sync, without
        # exposing the account email in returned metadata or local history.
        return ["--account", self._email] if self._email is not None else []

    def fetch(self, metric: str, date: str) -> Any:
        if not isinstance(metric, str) or metric not in METRICS:
            raise BackendError("invalid_input", "Unknown health metric.")
        try:
            parse_dates(date, date)
        except ValueError as error:
            raise BackendError("invalid_input", str(error)) from None
        return self._json([*METRICS[metric], date, "--json", *self._account_args()])

    def activities(self, limit: int) -> Any:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ACTIVITY_LIMIT:
            raise BackendError("invalid_input", "Activity limit must be between 1 and 1000.")
        return self._json(["activities", "list", "--limit", str(limit), "--json", *self._account_args()])

    def download(self, activity_id: str, fmt: str, path: Path) -> dict[str, Any]:
        if not isinstance(activity_id, str) or not re.fullmatch(r"[0-9]+", activity_id) or not activity_id.strip("0"):
            raise BackendError("invalid_input", "Activity ID must be a positive integer.")
        if not isinstance(fmt, str) or fmt not in DOWNLOAD_FORMATS:
            raise BackendError("invalid_input", "Unsupported activity format.")
        destination = Path(path).absolute()
        if os.path.lexists(destination):
            raise BackendError("invalid_input", "The destination already exists.")
        if not destination.parent.is_dir():
            raise BackendError("invalid_input", "The destination directory does not exist.")
        temporary_dir = None
        try:
            temporary_dir = Path(tempfile.mkdtemp(prefix=".garmcli-", dir=destination.parent))
            temporary_file = temporary_dir / f"activity.{fmt}"
            self._run([
                "activity", "download", activity_id, "--format", fmt,
                "--output", str(temporary_file), "--json", *self._account_args(),
            ])
            try:
                info = temporary_file.lstat()
            except FileNotFoundError:
                raise BackendError("download_failed", "gccli did not produce a valid activity file.") from None
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise BackendError("download_failed", "gccli did not produce a valid activity file.")
            temporary_file.chmod(0o600)
            # Hard-link publication is exclusive even if a destination appears
            # while the network request is in progress.
            os.link(temporary_file, destination)
            return {
                "path": str(destination),
                "bytes": info.st_size,
                "activity_id": activity_id,
                "format": fmt,
            }
        except FileExistsError:
            raise BackendError("invalid_input", "The destination already exists.") from None
        except OSError:
            raise BackendError("filesystem_error", "The local file operation failed.") from None
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
