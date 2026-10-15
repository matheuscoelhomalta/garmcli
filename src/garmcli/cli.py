"""Read-only command workflows and stable machine-readable responses."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

from .backend import BackendError, GarminBackend
from .auth import SessionStore, account_identity, login
from .contracts import METRICS, SCHEMA_VERSION, parse_dates
from .store import HistoryStore


class UsageError(Exception):
    pass


class CommandError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def default_db():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "garmcli" / "history.sqlite3"


def positive_float(value):
    result = nonnegative_float(value)
    if result == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def nonnegative_float(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a finite number") from None
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return result


def activity_limit(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000") from None
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000")
    return result


def activity_id(value):
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise argparse.ArgumentTypeError("activity ID must be a positive integer")
    return value


def parser():
    p = Parser(prog="garmcli", description="Read-only Garmin history and exports")
    p.add_argument("--json", action="store_true", help="emit a stable JSON envelope")
    p.add_argument("--db", type=Path, default=default_db())
    p.add_argument("--account-id")
    p.add_argument("--timeout", type=positive_float, default=120)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check setup without exposing credentials")
    auth = commands.add_parser("auth", help="manage the local Garmin session").add_subparsers(dest="action", required=True)
    signing_in = auth.add_parser("login", help="sign in with hidden password and MFA prompts")
    signing_in.add_argument("email")
    signing_in.add_argument("--domain", choices=["garmin.com", "garmin.cn"], default="garmin.com")
    auth.add_parser("status", help="check the saved session without a live request")
    auth.add_parser("logout", help="remove the selected local session; preserve history")
    devices = commands.add_parser("devices").add_subparsers(dest="action", required=True)
    devices.add_parser("list")
    sync = commands.add_parser("sync", help="fetch and persist an inclusive date/metric grid")
    date_range(sync)
    sync.add_argument("--metrics", default=",".join(METRICS))
    sync.add_argument("--refresh", action="store_true")
    sync.add_argument("--delay", type=nonnegative_float, default=0.5)
    health = commands.add_parser("health", help="read cached history; never fetch implicitly")
    health.add_argument("metric", choices=list(METRICS))
    date_range(health)
    health.add_argument("--offline", action="store_true", help="cached reads are always offline")
    health.add_argument("--out", type=Path)
    snapshot = commands.add_parser("snapshot", help="read a daily snapshot from local history")
    snapshot.add_argument("--date", required=True)
    snapshot.add_argument("--metrics", default="summary,sleep,hrv,training-readiness")
    snapshot.add_argument("--offline", action="store_true")
    activities = commands.add_parser("activities").add_subparsers(dest="action", required=True)
    listing = activities.add_parser("list")
    listing.add_argument("--limit", type=activity_limit, default=20)
    download = activities.add_parser("download")
    download.add_argument("activity_id", type=activity_id)
    download.add_argument("--format", choices=["fit", "gpx", "tcx", "kml", "csv"], required=True)
    download.add_argument("--out", type=Path, required=True)
    return p


def date_range(p):
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)


def select_metrics(value):
    selected = value.split(",")
    if not selected or any(metric not in METRICS for metric in selected):
        raise UsageError("metrics must be a comma-separated selection of: " + ", ".join(METRICS))
    return list(dict.fromkeys(selected))


def error_exit(error):
    code = str(error.get("code", "")).lower()
    if error.get("status") == 429 or code in {"rate_limited", "rate_limit", "rate_limit_exceeded", "too_many_requests"}:
        return 5
    if error.get("status") in (401, 403) or code in {"auth_required", "auth_expired", "auth_error", "unauthorized", "forbidden", "unauthenticated", "authentication_required"}:
        return 3
    if code == "invalid_input":
        return 2
    return 1


def envelope(command, data=None, errors=None, complete=None):
    errors = errors or []
    result = {"schema_version": SCHEMA_VERSION, "command": command,
              "ok": not errors, "data": data, "errors": errors}
    if complete is not None:
        result["complete"] = complete
        result["ok"] = result["ok"] and complete
    return result


def backend(args):
    return GarminBackend(timeout=args.timeout)


def auth_command(args):
    if args.action == "login":
        identity, _, _ = account_identity(args.email, args.domain)
        if args.account_id and args.account_id != identity:
            raise CommandError("account_mismatch", "--account-id does not match the requested Garmin account.")
        if not sys.stdin.isatty():
            raise BackendError("auth_required", "Run garmcli auth login EMAIL in an interactive terminal.")
        password = getpass.getpass("Garmin password: ", stream=sys.stderr)
        if not password:
            raise UsageError("Password cannot be empty.")
        result = login(args.email, password, domain=args.domain, timeout=args.timeout,
                       prompt_mfa=lambda: getpass.getpass("Garmin MFA code: ", stream=sys.stderr))
        return envelope("auth", result), 0
    store = SessionStore()
    status = store.status()
    if args.account_id and status.get("account_id") != args.account_id:
        raise CommandError("account_mismatch", "--account-id does not match the selected Garmin account.")
    if args.action == "logout":
        status = store.delete()
    return envelope("auth", status), 0


def cached_account(store, args):
    if args.account_id:
        return args.account_id
    accounts = store.accounts()
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise CommandError("account_required", "No cached account; specify --account-id or sync first.")
    raise CommandError("account_required", "Multiple cached accounts; specify --account-id.")


def grid_records(store, account, dates, metrics):
    return [store.get(account, metric, date) for date in dates for metric in metrics]


def record_errors(records):
    errors = [dict(record["error"], date=record["date"], metric=record["metric"])
              for record in records if record["status"] == "error"]
    missing = sum(record["status"] == "missing" for record in records)
    if missing:
        errors.append({"code": "incomplete_history", "message": f"{missing} requested records are missing."})
    return errors


def sync_history(args, dates, metrics):
    live = backend(args)
    # Even explicit account selection cannot redirect live data to a different account.
    account = live.account_id()
    if args.account_id and args.account_id != account:
        raise CommandError("account_mismatch", "--account-id does not match the authenticated Garmin account.")
    fetched = skipped = 0
    interrupted = False
    stopped = None
    store = HistoryStore(args.db)
    try:
        try:
            for date in dates:
                for metric in metrics:
                    previous = store.get(account, metric, date)
                    if not args.refresh and previous["status"] in ("available", "empty"):
                        skipped += 1
                        continue
                    if fetched and args.delay:
                        time.sleep(args.delay)
                    try:
                        payload = live.fetch(metric, date)
                    except BackendError as exc:
                        record = store.save(account, metric, date, error=exc.as_dict())
                    else:
                        record = store.save(account, metric, date, data=payload)
                    fetched += 1
                    print(f"{date} {metric}: {record['status']} ({fetched} fetched, {skipped} skipped)",
                          file=sys.stderr, flush=True)
                    if record["status"] == "error" and (
                        error_exit(record["error"]) in (3, 5)
                        or record["error"]["code"] in {"credential_store_unavailable", "auth_storage_error", "filesystem_error"}
                    ):
                        stopped = record["error"]
                        break
                if stopped:
                    break
        except KeyboardInterrupt:
            interrupted = True
        records = grid_records(store, account, dates, metrics)
        counts = {"requested": len(records), "fetched": fetched, "skipped": skipped}
        counts.update({key: sum(r["status"] == status for r in records)
                       for key, status in [("available", "available"), ("empty", "empty"),
                                           ("errors", "error"), ("missing", "missing")]})
        complete = not interrupted and counts["available"] + counts["empty"] == counts["requested"]
        errors = record_errors(records)
        if interrupted:
            errors.append({"code": "interrupted", "message": "Sync interrupted; completed records are saved. Resume with the same range and metrics."})
        data = {"account_id": account, "from": dates[0], "to": dates[-1], "metrics": metrics, "counts": counts}
        data["interrupted"] = interrupted
        result = envelope("sync", data, errors, complete)
        code = 130 if interrupted else error_exit(stopped) if stopped else 0 if result["ok"] else 1
        return result, code
    finally:
        store.close()


def read_history(args, dates, metrics):
    store = HistoryStore(args.db)
    try:
        account = cached_account(store, args)
        records = grid_records(store, account, dates, metrics)
    finally:
        store.close()
    complete = all(r["status"] in ("available", "empty") for r in records)
    data = {"account_id": account, "from": dates[0], "to": dates[-1], "metrics": metrics, "records": records}
    result = envelope(args.command, data, record_errors(records), complete)
    return result, 0 if result["ok"] else 1


def run(args):
    if args.account_id is not None and not args.account_id.strip():
        raise UsageError("--account-id cannot be empty")
    if args.command == "auth":
        return auth_command(args)
    if args.command in ("sync", "health"):
        try:
            dates = parse_dates(args.start, args.end)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
        metrics = select_metrics(args.metrics) if args.command == "sync" else [args.metric]
        if args.command == "sync":
            return sync_history(args, dates, metrics)
        return read_history(args, dates, metrics)
    if args.command == "snapshot":
        try:
            dates = parse_dates(args.date, args.date)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
        return read_history(args, dates, select_metrics(args.metrics))
    if args.command == "doctor":
        live = backend(args)
        data = {"backend": {"name": "garminconnect", "available": False}, "authenticated": False, "credentials_available": False,
                "history": {"path": str(args.db), "exists": args.db.exists(), "ready": False}}
        errors = []
        try:
            data["backend"] = {"name": "garminconnect", "available": True, "version": live.version()}
            auth = live.auth_status()
            for key in ("authenticated", "expired", "domain", "expires_at"):
                if key in auth:
                    data[key] = auth[key]
            if not auth.get("credentials_available", auth.get("authenticated", False)):
                raise BackendError("auth_required", "No Garmin session; run garmcli auth login EMAIL.")
            data["account_id"] = live.account_id()
            data["credentials_available"] = True
        except BackendError as exc:
            errors.append(exc.as_dict())
        try:
            store = HistoryStore(args.db)
            store.close()
            data["history"]["ready"] = True
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            errors.append({"code": "history_unavailable", "message": "History database cannot be opened; check path, permissions, and schema."})
        result = envelope("doctor", data, errors)
        code = next((error_exit(error) for error in errors if error_exit(error) in (3, 5)), 1 if errors else 0)
        return result, code
    live = backend(args)
    if args.command == "devices":
        return envelope("devices", live.devices()), 0
    if args.action == "list":
        return envelope("activities", live.activities(args.limit)), 0
    return envelope("activities", live.download(args.activity_id, args.format, args.out)), 0


def write_export(path, result):
    # O_EXCL makes refusal to overwrite atomic, including against concurrent writers.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def emit(result, json_mode):
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        print(f"{result['command']}: {'ok' if result['ok'] else 'failed'}")
        if result["data"] is not None:
            print(json.dumps(result["data"], ensure_ascii=False, indent=2, allow_nan=False))
        for error in result["errors"]:
            print(f"{error['code']}: {error['message']}", file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    command = next((item for item in argv if item in {"doctor", "auth", "devices", "sync", "health", "snapshot", "activities"}), "usage")
    try:
        args = parser().parse_args(argv)
        command = args.command
        result, code = run(args)
        if args.command == "health" and args.out:
            write_export(args.out, result)
    except UsageError as exc:
        result, code = envelope(command, errors=[{"code": "invalid_input", "message": str(exc)}]), 2
    except BackendError as exc:
        error = exc.as_dict()
        result, code = envelope(command, errors=[error]), error_exit(error)
    except CommandError as exc:
        result, code = envelope(command, errors=[{"code": exc.code, "message": exc.message}]), 1
    except FileExistsError:
        result, code = envelope(command, errors=[{"code": "destination_exists", "message": "Destination exists; choose a new output path."}]), 1
    except KeyboardInterrupt:
        result, code = envelope(command, errors=[{"code": "interrupted", "message": "Operation interrupted."}]), 130
    except EOFError:
        result, code = envelope(command, errors=[{"code": "auth_required", "message": "Login input was closed; run auth login in an interactive terminal."}]), 3
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Local exceptions may include paths or credential-bearing library text.
        result, code = envelope(command, errors=[{"code": "local_error", "message": "Local operation failed; check history/output paths and setup."}]), 1
    emit(result, json_mode)
    return code
