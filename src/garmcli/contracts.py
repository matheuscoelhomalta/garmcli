"""Shared validation and JSON record contracts."""

from datetime import date as calendar_date, datetime, timedelta, timezone
import re
from typing import Any


SCHEMA_VERSION = "1"

METRICS = (
    "summary", "steps", "heart-rate", "floors", "sleep", "stress",
    "respiration", "spo2", "hrv", "body-battery", "intensity-minutes",
    "training-readiness", "training-status", "fitness-age", "max-metrics",
)

_STATUSES = {"available", "empty", "error", "missing"}
_SAFE_ERROR_CODES = {
    "auth_required", "auth_expired", "auth_error", "unauthorized", "forbidden",
    "rate_limited", "rate_limit", "too_many_requests", "not_found",
    "bad_request", "invalid_input", "invalid_json", "executable_missing",
    "timeout", "network_error", "server_error", "api_error", "backend_error",
    "upstream_error", "filesystem_error", "download_failed", "interrupted",
    "history_error", "incomplete", "usage_error", "error", "dependency_missing",
    "credential_store_unavailable", "auth_storage_error",
}


def parse_dates(start: str, end: str) -> list[str]:
    """Return strictly formatted inclusive dates, with no date shortcuts."""
    values = []
    for value in (start, end):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Dates must use YYYY-MM-DD.")
        try:
            values.append(calendar_date.fromisoformat(value))
        except ValueError:
            raise ValueError("Dates must be valid calendar dates.") from None
    first, last = values
    if first > last:
        raise ValueError("Start date must be on or before end date.")
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def is_empty(data: Any) -> bool:
    """Only explicit top-level absence is empty; zero-valued data is present."""
    return data is None or (isinstance(data, (dict, list)) and len(data) == 0)


def safe_error(error: Any) -> dict[str, Any]:
    """Normalize external failures without retaining arbitrary server messages."""
    if not isinstance(error, dict):
        error = {}
    code = error.get("code")
    if not isinstance(code, str) or code not in _SAFE_ERROR_CODES:
        code = "upstream_error"
    status = error.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    if code in {"auth_required", "auth_expired", "auth_error", "unauthorized"} or status == 401:
        message = "Garmin authentication is required."
    elif code in {"rate_limited", "rate_limit", "too_many_requests"} or status == 429:
        message = "Garmin rate limit reached."
    else:
        message = {
            "invalid_input": "Invalid input.",
            "usage_error": "Invalid command arguments.",
            "invalid_json": "Garmin returned invalid JSON.",
            "executable_missing": "A required executable was not found.",
            "dependency_missing": "Required Python dependencies are missing; reinstall garmcli.",
            "credential_store_unavailable": "The system credential store is unavailable or locked.",
            "auth_storage_error": "The local session could not be stored securely.",
            "timeout": "Garmin request timed out.",
            "filesystem_error": "The local file operation failed.",
            "download_failed": "Garmin did not produce a valid activity file.",
            "interrupted": "The operation was interrupted.",
            "history_error": "The local history operation failed.",
            "incomplete": "The requested data is incomplete.",
        }.get(code, "Garmin request failed.")
        if status is not None:
            message = f"Garmin request failed (HTTP {status})."
    result = {"code": code, "message": message}
    if status is not None:
        result["status"] = status
    return result


def make_record(
    date: str,
    metric: str,
    status: str,
    data: Any = None,
    error: Any = None,
    fetched_at: str | None = None,
    attempts: int = 0,
) -> dict[str, Any]:
    if status not in _STATUSES:
        raise ValueError("Unknown record status.")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("Attempts must be a nonnegative integer.")
    if fetched_at is None and status != "missing":
        fetched_at = datetime.now(timezone.utc).isoformat()
    return {
        "date": date,
        "metric": metric,
        "status": status,
        "data": data,
        "error": safe_error(error) if error is not None else None,
        "fetched_at": fetched_at,
        "attempts": attempts,
    }
