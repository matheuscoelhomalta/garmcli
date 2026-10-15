"""Internal, fixed GET allowlist; the parent enforces the total deadline."""

import base64
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import json
import logging
import math
import random
import re
import sys
import time
from urllib.parse import quote
import zipfile

from .backend import DOWNLOAD_FORMATS, MAX_ACTIVITY_LIMIT, strict_json
from .contracts import METRICS, parse_dates, safe_error
from .errors import BackendError


# These paths deliberately match the old raw responses, including daily maxmet
# and aggregated training status. SDK convenience methods transform some data.
_DATED_PATHS = {
    "floors": "/wellness-service/wellness/floorsChartData/daily/{date}",
    "stress": "/wellness-service/wellness/dailyStress/{date}",
    "respiration": "/wellness-service/wellness/daily/respiration/{date}",
    "spo2": "/wellness-service/wellness/daily/spo2/{date}",
    "hrv": "/hrv-service/hrv/{date}",
    "intensity-minutes": "/wellness-service/wellness/daily/im/{date}",
    "training-readiness": "/metrics-service/metrics/trainingreadiness/{date}",
    "training-status": "/metrics-service/metrics/trainingstatus/aggregated/{date}",
    "fitness-age": "/fitnessage-service/fitnessage/{date}",
    "max-metrics": "/metrics-service/metrics/maxmet/daily/{date}/{date}",
}
_PROFILE_PATHS = {
    "summary": "/usersummary-service/usersummary/daily/",
    "steps": "/wellness-service/wellness/dailySummaryChart/",
    "heart-rate": "/wellness-service/wellness/dailyHeartRate/",
    "sleep": "/wellness-service/wellness/dailySleepData/",
}


def request_target(request, record):
    operation = request.get("operation")
    if operation == "devices":
        return "/device-service/deviceregistration/devices", {}
    if operation == "activities":
        limit = request.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ACTIVITY_LIMIT:
            raise BackendError("invalid_input", "Invalid activity limit.")
        return "/activitylist-service/activities/search/activities", {"start": 0, "limit": limit}
    if operation == "download":
        activity_id, fmt = request.get("activity_id"), request.get("format")
        if not isinstance(activity_id, str) or not re.fullmatch(r"[0-9]+", activity_id) or not activity_id.strip("0"):
            raise BackendError("invalid_input", "Invalid activity ID.")
        if not isinstance(fmt, str) or fmt not in DOWNLOAD_FORMATS:
            raise BackendError("invalid_input", "Unsupported activity format.")
        if fmt == "fit":
            return f"/download-service/files/activity/{activity_id}", {}
        return f"/download-service/export/{fmt}/activity/{activity_id}", {}
    if operation != "health":
        raise BackendError("invalid_input", "Unknown operation.")
    metric, date = request.get("metric"), request.get("date")
    if not isinstance(metric, str) or metric not in METRICS:
        raise BackendError("invalid_input", "Unknown health metric.")
    try:
        parse_dates(date, date)
    except ValueError:
        raise BackendError("invalid_input", "Invalid calendar date.") from None
    if metric in _DATED_PATHS:
        return _DATED_PATHS[metric].format(date=date), {}
    if metric == "body-battery":
        return "/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": date, "endDate": date}
    display_name = record.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        raise BackendError("auth_required", "Garmin authentication is required.")
    params = {"calendarDate" if metric == "summary" else "date": date}
    if metric == "sleep":
        params["nonSleepBufferMinutes"] = 60
    return _PROFILE_PATHS[metric] + quote(display_name, safe=""), params


def retry_after(value):
    try:
        seconds = int(value)
        if seconds > 0:
            return seconds
    except (TypeError, ValueError):
        pass
    try:
        at = parsedate_to_datetime(value)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(1, (at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 1


class RetrySession:
    """Inspect responses before the pinned SDK discards Retry-After headers."""

    def __init__(self, session, deadline):
        self.session = session
        self.deadline = deadline

    def request(self, method, url, **kwargs):
        if method != "GET":
            raise BackendError("invalid_input", "Only read requests are permitted.")
        rates = servers = 0
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("timeout", "Garmin request timed out.")
            kwargs["timeout"] = remaining
            response = self.session.request(method, url, **kwargs)
            status = response.status_code
            if status == 429:
                delay = retry_after(response.headers.get("Retry-After"))
                if rates >= 3 or delay > 30:
                    return response
                rates += 1
            elif status >= 500:
                if servers >= 2:
                    return response
                delay = 0.5 * 2 ** servers * (1 + random.random() * 0.25)
                servers += 1
            else:
                return response
            response.close()
            if delay >= self.deadline - time.monotonic():
                raise BackendError("timeout", "Garmin request timed out.")
            time.sleep(delay)


def response_data(response, operation, fmt=None):
    contents = response.content
    if operation == "download":
        if fmt == "fit":
            try:
                with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                    member = next((item for item in archive.infolist()
                                   if item.filename.rsplit("/", 1)[-1].casefold().endswith(".fit")), None)
                    if member is None:
                        raise BackendError("download_failed", "Garmin did not produce a valid activity file.")
                    contents = archive.read(member)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError):
                raise BackendError("download_failed", "Garmin did not produce a valid activity file.") from None
        if not contents:
            raise BackendError("download_failed", "Garmin did not produce a valid activity file.")
        return {"content": base64.b64encode(contents).decode("ascii")}
    if not contents:
        return None if operation == "activities" else {}
    try:
        return strict_json(contents)
    except (ValueError, TypeError, UnicodeError):
        raise BackendError("invalid_json", "Garmin returned invalid JSON.") from None


def execute(request):
    from .auth import SessionStore, create_client, persist_client

    if not isinstance(request, dict):
        raise BackendError("invalid_input", "Invalid worker request.")
    identity = request.get("account_id")
    timeout = request.get("timeout")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise BackendError("invalid_input", "Invalid account identity.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise BackendError("invalid_input", "Invalid timeout.")
    # Validate the operation before any credential lookup. Profile endpoints are
    # checked again against the actual stored display name after loading.
    request_target(request, {"display_name": "validation"})
    deadline = time.monotonic() + timeout
    store = SessionStore()
    with store.lock(identity):
        record = store.load(identity)
        target, params = request_target(request, record)
        api = create_client(record, timeout=timeout, store=store)
        native = api.client
        native._api_session = RetrySession(native._api_session, deadline)
        try:
            # Match the SDK download helper's content negotiation; its default
            # API headers request JSON, which binary/export endpoints can reject.
            headers = {"Accept": "*/*"} if request["operation"] == "download" else {}
            response = native.request("GET", "connectapi", target, params=params, timeout=timeout, headers=headers)
            try:
                return response_data(response, request["operation"], request.get("format"))
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        finally:
            persist_client(api, record, store)


def error_details(error):
    if isinstance(error, BackendError):
        return safe_error(error.as_dict())
    status = None
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidate = getattr(current, "status_code", None)
        if candidate is None:
            candidate = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and 100 <= candidate <= 599:
            status = candidate
            break
        match = re.match(r"API Error ([1-5][0-9]{2})\b", str(current))
        if match:
            status = int(match.group(1))
            break
        current = current.__cause__ or current.__context__
    name = type(error).__name__
    if status == 401 or name == "GarminConnectAuthenticationError":
        code = "auth_required"
    elif status == 429 or name == "GarminConnectTooManyRequestsError":
        code = "rate_limited"
    elif status == 404 or name == "GarminConnectNotFoundError":
        code = "not_found"
    elif name in {"Timeout", "ReadTimeout", "ConnectTimeout"}:
        code = "timeout"
    elif name == "ConnectionError":
        code = "network_error"
    else:
        code = "error" if status is not None else "backend_error"
    return safe_error({"code": code, "status": status})


class _DiscardOutput:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def main():
    logging.disable(logging.CRITICAL)
    try:
        request = strict_json(sys.stdin.buffer.read())
        # The worker protocol is its only output; third-party text never reaches
        # the parent, including during token refresh and malformed responses.
        with contextlib.redirect_stdout(_DiscardOutput()), contextlib.redirect_stderr(_DiscardOutput()):
            data = execute(request)
        response = {"ok": True, "data": data}
    except Exception as error:
        response = {"ok": False, "error": error_details(error)}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
