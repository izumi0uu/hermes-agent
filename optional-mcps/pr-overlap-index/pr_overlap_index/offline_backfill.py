"""Offline backfill and import-bundle helpers for the PR overlap index."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from .core import (
    activate_cold_import_bundle,
    export_cold_import_bundle,
    health_snapshot,
    import_disk_preflight,
    restore_import_backup,
    validate_cold_import_bundle,
)
from .indexer import (
    DEFAULT_MAX_RSS_MB,
    DEFAULT_MAX_RUNTIME_SECONDS,
    GitHubRestClient,
    PullRequestClient,
    RateLimitedGitHubError,
    TransientGitHubError,
    run_one_shot_indexer,
)

DEFAULT_REPO = "NousResearch/hermes-agent"


def _compact_pass(summary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "run_id",
        "scope",
        "indexed",
        "requests_used",
        "capped",
        "stop_reason",
        "elapsed_seconds",
        "db_bytes",
        "wal_bytes",
        "free_disk_bytes",
        "file_fetch_errors",
        "resume_hint",
    )
    return {key: summary.get(key) for key in keys if key in summary}


def _count_health(db: str | Path) -> dict[str, Any]:
    try:
        health = health_snapshot(db)
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__, "detail": str(exc)}
    return {
        "ok": bool(health.get("ok")),
        "indexed_pr_count": health.get("indexed_pr_count"),
        "latest_live_manifest_count": health.get("latest_live_manifest_count"),
        "coverage_by_scope": health.get("coverage_by_scope"),
        "last_run_by_scope": health.get("last_run_by_scope"),
        "brownout_state": health.get("brownout_state"),
        "db_bytes": health.get("db_bytes"),
        "wal_bytes": health.get("wal_bytes"),
        "free_disk_bytes": health.get("free_disk_bytes"),
    }


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TransientGitHubError, RateLimitedGitHubError)):
        return True
    text = f"{type(exc).__name__}: {exc}"
    return any(
        marker in text
        for marker in (
            "GitHub transient error after retries",
            "RemoteDisconnected",
            "IncompleteRead",
            "UNEXPECTED_EOF_WHILE_READING",
            "Connection reset",
            "timed out",
        )
    )


def _reconnect_sleep_seconds(
    failures: int,
    *,
    base_sleep: float,
    max_sleep: float,
    retry_after_seconds: int | None = None,
) -> float:
    if retry_after_seconds is not None:
        return min(max_sleep, max(0.0, float(retry_after_seconds)))
    return min(max_sleep, base_sleep * (2 ** max(0, failures - 1)))


def run_backfill_loop(
    db: str | Path,
    repo: str = DEFAULT_REPO,
    *,
    client: PullRequestClient | None = None,
    scope: str = "open",
    max_passes: int = 1000,
    max_total_requests: int | None = None,
    max_elapsed_seconds: int | None = None,
    no_progress_limit: int = 3,
    batch_limit: int = 300,
    max_requests: int = 80,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    max_rss_mb: int = DEFAULT_MAX_RSS_MB,
    min_free_disk_bytes: int = 4 * 1024 * 1024 * 1024,
    max_files_per_pr: int = 30,
    max_patch_bytes: int = 2048,
    bundle_dir: str | Path | None = None,
    reconnect_limit: int = 20,
    reconnect_base_sleep: float = 10.0,
    reconnect_max_sleep: float = 300.0,
    export_on_transient_error: bool = True,
    export_every_passes: int = 5,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Run bounded indexer passes until complete or an explicit cap is reached.

    This is intended for an offline builder or temporary worker. It does not
    activate anything on the serving VPS. If ``bundle_dir`` is supplied, the
    resulting DB is exported as a validated import bundle after the loop stops.
    """
    if max_passes < 1:
        raise ValueError("max_passes must be >= 1")
    if no_progress_limit < 1:
        raise ValueError("no_progress_limit must be >= 1")
    if reconnect_limit < 0:
        raise ValueError("reconnect_limit must be >= 0")
    if reconnect_base_sleep < 0:
        raise ValueError("reconnect_base_sleep must be >= 0")
    if reconnect_max_sleep < 0:
        raise ValueError("reconnect_max_sleep must be >= 0")

    started = time.monotonic()
    request_total = 0
    indexed_total = 0
    no_progress = 0
    reconnect_failures = 0
    passes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    interim_exports: list[dict[str, Any]] = []
    stop_reason = "max_passes"
    capped = True
    indexer_client = client or GitHubRestClient(os.environ.get("GITHUB_TOKEN"))

    before = _count_health(db)
    pass_index = 0
    while pass_index < max_passes:
        pass_index += 1
        if max_elapsed_seconds is not None:
            if time.monotonic() - started >= max_elapsed_seconds:
                stop_reason = "elapsed_cap"
                break
        if max_total_requests is not None and request_total >= max_total_requests:
            stop_reason = "request_cap"
            break

        pass_client = indexer_client
        if client is None:
            pass_client = GitHubRestClient(os.environ.get("GITHUB_TOKEN"))
        try:
            summary = run_one_shot_indexer(
                db,
                repo,
                client=pass_client,
                scope=scope,
                max_requests=max_requests,
                max_runtime_seconds=max_runtime_seconds,
                max_rss_mb=max_rss_mb,
                min_free_disk_bytes=min_free_disk_bytes,
                batch_limit=batch_limit,
                max_files_per_pr=max_files_per_pr,
                max_patch_bytes=max_patch_bytes,
            )
        except KeyboardInterrupt:
            stop_reason = "interrupted"
            capped = True
            errors.append({
                "pass": pass_index,
                "type": "KeyboardInterrupt",
                "detail": "interrupted by caller",
            })
            break
        except Exception as exc:
            if not _is_transient_exception(exc):
                raise
            reconnect_failures += 1
            stop_reason = "transient_error"
            errors.append({
                "pass": pass_index,
                "type": type(exc).__name__,
                "detail": str(exc),
                "reconnect_attempt": reconnect_failures,
            })
            if bundle_dir is not None and export_on_transient_error:
                interim_export = export_cold_import_bundle(db, bundle_dir, repo=repo)
                interim_exports.append({
                    "pass": pass_index,
                    "reason": "transient_error",
                    "ok": bool(interim_export.get("ok")),
                    "export": interim_export,
                })
                if not interim_export.get("ok"):
                    stop_reason = "export_failed"
                    break
            if reconnect_failures > reconnect_limit:
                stop_reason = "transient_error"
                break
            sleep_for = _reconnect_sleep_seconds(
                reconnect_failures,
                base_sleep=reconnect_base_sleep,
                max_sleep=reconnect_max_sleep,
            )
            errors[-1]["sleep_seconds"] = sleep_for
            sleep_fn(sleep_for)
            continue
        compact = _compact_pass(summary)
        compact["pass"] = pass_index
        passes.append(compact)

        indexed = int(summary.get("indexed") or 0)
        requests = int(summary.get("requests_used") or 0)
        indexed_total += indexed
        request_total += requests

        pass_stop = str(summary.get("stop_reason") or "")
        if pass_stop in {"transient_network", "rate_limited"}:
            reconnect_failures += 1
            stop_reason = (
                "transient_error"
                if pass_stop == "transient_network"
                else "rate_limited"
            )
            error = summary.get("error") or {}
            retry_after = error.get("retry_after_seconds")
            errors.append({
                "pass": pass_index,
                "type": error.get("type") or pass_stop,
                "detail": error.get("detail") or pass_stop,
                "reconnect_attempt": reconnect_failures,
            })
            if bundle_dir is not None and export_on_transient_error:
                interim_export = export_cold_import_bundle(db, bundle_dir, repo=repo)
                interim_exports.append({
                    "pass": pass_index,
                    "reason": pass_stop,
                    "ok": bool(interim_export.get("ok")),
                    "export": interim_export,
                })
                if not interim_export.get("ok"):
                    stop_reason = "export_failed"
                    break
            if reconnect_failures > reconnect_limit:
                break
            sleep_for = _reconnect_sleep_seconds(
                reconnect_failures,
                base_sleep=reconnect_base_sleep,
                max_sleep=reconnect_max_sleep,
                retry_after_seconds=retry_after
                if isinstance(retry_after, int)
                else None,
            )
            errors[-1]["sleep_seconds"] = sleep_for
            sleep_fn(sleep_for)
            continue

        reconnect_failures = 0

        if indexed <= 0:
            no_progress += 1
        else:
            no_progress = 0

        if not summary.get("capped"):
            stop_reason = "index_complete"
            capped = False
            break
        if no_progress >= no_progress_limit:
            stop_reason = "no_progress"
            break
        if max_total_requests is not None and request_total >= max_total_requests:
            stop_reason = "request_cap"
            break
        if (
            bundle_dir is not None
            and export_every_passes > 0
            and pass_index % export_every_passes == 0
        ):
            interim_export = export_cold_import_bundle(db, bundle_dir, repo=repo)
            interim_exports.append({
                "pass": pass_index,
                "reason": "periodic",
                "ok": bool(interim_export.get("ok")),
                "export": interim_export,
            })
            if not interim_export.get("ok"):
                stop_reason = "export_failed"
                capped = True
                break

    after = _count_health(db)
    export: dict[str, Any] | None = None
    if bundle_dir is not None:
        export = export_cold_import_bundle(db, bundle_dir, repo=repo)
        if not export.get("ok"):
            stop_reason = "export_failed"
            capped = True
    ok = stop_reason not in {
        "no_progress",
        "export_failed",
        "transient_error",
        "rate_limited",
        "interrupted",
    }
    if export is not None:
        ok = ok and bool(export.get("ok"))

    return {
        "ok": ok,
        "repo": repo,
        "db": str(db),
        "scope": scope,
        "stop_reason": stop_reason,
        "capped": capped,
        "passes": len(passes),
        "indexed_total": indexed_total,
        "requests_used_total": request_total,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "before": before,
        "after": after,
        "recent_passes": passes[-10:],
        "errors": errors[-5:],
        "reconnect_failures": reconnect_failures,
        "interim_exports": interim_exports[-5:],
        "export": export,
    }


def _json_print(value: dict[str, Any]) -> int:
    print(json.dumps(value, sort_keys=True))
    return 0 if value.get("ok") else 1


def _bytes_from_gb(value: float) -> int:
    return int(value * 1024 * 1024 * 1024)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline PR-overlap backfill and safe import-bundle admin."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser(
        "backfill-open",
        help="Run repeated open-PR indexer passes and optionally export a bundle.",
    )
    backfill.add_argument("repo", nargs="?", default=DEFAULT_REPO)
    backfill.add_argument("--db", required=True)
    backfill.add_argument("--bundle-dir")
    backfill.add_argument("--max-passes", type=int, default=1000)
    backfill.add_argument("--max-total-requests", type=int)
    backfill.add_argument("--max-elapsed-seconds", type=int)
    backfill.add_argument("--no-progress-limit", type=int, default=3)
    backfill.add_argument("--batch-limit", type=int, default=300)
    backfill.add_argument("--max-requests", type=int, default=80)
    backfill.add_argument(
        "--max-runtime-seconds", type=int, default=DEFAULT_MAX_RUNTIME_SECONDS
    )
    backfill.add_argument("--max-rss-mb", type=int, default=DEFAULT_MAX_RSS_MB)
    backfill.add_argument("--min-free-disk-gb", type=float, default=4.0)
    backfill.add_argument("--max-files-per-pr", type=int, default=30)
    backfill.add_argument("--max-patch-bytes", type=int, default=2048)
    backfill.add_argument("--reconnect-limit", type=int, default=20)
    backfill.add_argument("--reconnect-base-sleep", type=float, default=10.0)
    backfill.add_argument("--reconnect-max-sleep", type=float, default=300.0)
    backfill.add_argument("--export-every-passes", type=int, default=5)
    backfill.add_argument(
        "--no-export-on-transient-error",
        action="store_true",
        help="Do not refresh the bundle before sleeping after a transient error.",
    )

    export = sub.add_parser("export-bundle", help="Export a validated DB bundle.")
    export.add_argument("repo", nargs="?", default=DEFAULT_REPO)
    export.add_argument("--db", required=True)
    export.add_argument("--bundle-dir", required=True)

    validate = sub.add_parser("validate-bundle", help="Validate a staged bundle.")
    validate.add_argument("--bundle-dir", required=True)
    validate.add_argument("--expected-repo", default=DEFAULT_REPO)

    preflight = sub.add_parser("preflight", help="Check disk headroom before activate.")
    preflight.add_argument("--live-db", required=True)
    preflight.add_argument("--staging-db", required=True)
    preflight.add_argument("--min-free-disk-gb", type=float, default=4.0)

    activate = sub.add_parser("activate-bundle", help="Activate a validated bundle.")
    activate.add_argument("--live-db", required=True)
    activate.add_argument("--bundle-dir", required=True)
    activate.add_argument("--expected-repo", default=DEFAULT_REPO)
    activate.add_argument("--rollback-dir")
    activate.add_argument("--min-free-disk-gb", type=float, default=4.0)

    restore = sub.add_parser("restore-backup", help="Restore an activation rollback DB.")
    restore.add_argument("--live-db", required=True)
    restore.add_argument("--rollback-db", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "backfill-open":
        return _json_print(
            run_backfill_loop(
                args.db,
                args.repo,
                scope="open",
                max_passes=args.max_passes,
                max_total_requests=args.max_total_requests,
                max_elapsed_seconds=args.max_elapsed_seconds,
                no_progress_limit=args.no_progress_limit,
                batch_limit=args.batch_limit,
                max_requests=args.max_requests,
                max_runtime_seconds=args.max_runtime_seconds,
                max_rss_mb=args.max_rss_mb,
                min_free_disk_bytes=_bytes_from_gb(args.min_free_disk_gb),
                max_files_per_pr=args.max_files_per_pr,
                max_patch_bytes=args.max_patch_bytes,
                bundle_dir=args.bundle_dir,
                reconnect_limit=args.reconnect_limit,
                reconnect_base_sleep=args.reconnect_base_sleep,
                reconnect_max_sleep=args.reconnect_max_sleep,
                export_on_transient_error=not args.no_export_on_transient_error,
                export_every_passes=args.export_every_passes,
            )
        )
    if args.command == "export-bundle":
        return _json_print(export_cold_import_bundle(args.db, args.bundle_dir, repo=args.repo))
    if args.command == "validate-bundle":
        return _json_print(
            validate_cold_import_bundle(
                args.bundle_dir, expected_repo=args.expected_repo
            )
        )
    if args.command == "preflight":
        return _json_print(
            import_disk_preflight(
                args.live_db,
                args.staging_db,
                min_free_disk_bytes=_bytes_from_gb(args.min_free_disk_gb),
            )
        )
    if args.command == "activate-bundle":
        return _json_print(
            activate_cold_import_bundle(
                args.live_db,
                args.bundle_dir,
                expected_repo=args.expected_repo,
                rollback_dir=args.rollback_dir,
                min_free_disk_bytes=_bytes_from_gb(args.min_free_disk_gb),
            )
        )
    if args.command == "restore-backup":
        return _json_print(restore_import_backup(args.live_db, args.rollback_db))
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
