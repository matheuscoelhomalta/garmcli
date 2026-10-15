"""Read-only Garmin operations with a bounded native Python worker."""

import base64
import binascii
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .contracts import METRICS, parse_dates, safe_error
from .errors import BackendError


DOWNLOAD_FORMATS = frozenset({"fit", "gpx", "tcx", "kml", "csv"})
MAX_ACTIVITY_LIMIT = 1000


def strict_json(value):
    def reject_constant(_value):
        raise ValueError("Non-JSON numeric constant.")

    return json.loads(value, parse_constant=reject_constant)


class GarminBackend:
    def __init__(self, timeout: float = 120):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise BackendError("invalid_input", "Timeout must be a positive finite number.")
        self.timeout = timeout
        self._auth: dict[str, Any] | None = None

    def version(self) -> str:
        try:
            return metadata.version("garminconnect")
        except metadata.PackageNotFoundError:
            raise BackendError("dependency_missing", "The Garmin Python library is not installed.") from None

    def auth_status(self) -> dict[str, Any]:
        if self._auth is None:
            from .auth import SessionStore

            self._auth = SessionStore().status()
        return dict(self._auth)

    def account_id(self) -> str:
        identity = self.auth_status().get("account_id")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise BackendError("auth_required", "Garmin authentication is required.")
        return identity

    def _request(self, operation: str, **parameters) -> Any:
        request = {"operation": operation, "account_id": self.account_id(),
                   "timeout": self.timeout, **parameters}
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "garmcli.worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            output, _ = process.communicate(
                json.dumps(request, allow_nan=False).encode("utf-8"), timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendError("timeout", "Garmin request timed out.") from None
        except OSError:
            raise BackendError("backend_error", "The Garmin worker could not be started.") from None
        finally:
            # communicate() can be interrupted by SIGINT or exceed its deadline.
            # Always reap the worker before returning control to the CLI.
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        try:
            response = strict_json(output)
        except (ValueError, TypeError, UnicodeError):
            raise BackendError("invalid_json", "The Garmin worker returned invalid JSON.") from None
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise BackendError("backend_error", "The Garmin worker failed.")
        if not response["ok"]:
            error = safe_error(response.get("error"))
            raise BackendError(error["code"], error["message"], error.get("status"))
        if process.returncode or "data" not in response:
            raise BackendError("backend_error", "The Garmin worker failed.")
        return response["data"]

    def devices(self) -> Any:
        return self._request("devices")

    def fetch(self, metric: str, date: str) -> Any:
        if not isinstance(metric, str) or metric not in METRICS:
            raise BackendError("invalid_input", "Unknown health metric.")
        try:
            parse_dates(date, date)
        except ValueError as error:
            raise BackendError("invalid_input", str(error)) from None
        return self._request("health", metric=metric, date=date)

    def activities(self, limit: int) -> Any:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ACTIVITY_LIMIT:
            raise BackendError("invalid_input", "Activity limit must be between 1 and 1000.")
        return self._request("activities", limit=limit)

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
            payload = self._request("download", activity_id=activity_id, format=fmt)
            try:
                contents = base64.b64decode(payload["content"], validate=True)
            except (TypeError, KeyError, ValueError, binascii.Error):
                raise BackendError("download_failed", "Garmin did not produce a valid activity file.") from None
            if not contents:
                raise BackendError("download_failed", "Garmin did not produce a valid activity file.")
            temporary_dir = Path(tempfile.mkdtemp(prefix=".garmcli-", dir=destination.parent))
            temporary_file = temporary_dir / f"activity.{fmt}"
            with temporary_file.open("xb") as handle:
                handle.write(contents)
            temporary_file.chmod(0o600)
            # Hard-link publication refuses a destination created during download.
            os.link(temporary_file, destination)
            return {"path": str(destination), "bytes": len(contents),
                    "activity_id": activity_id, "format": fmt}
        except FileExistsError:
            raise BackendError("invalid_input", "The destination already exists.") from None
        except OSError:
            raise BackendError("filesystem_error", "The local file operation failed.") from None
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
