"""Bounded one-shot GitHub PR indexer.

This module is intentionally small and stdlib-only.  It is designed for a
systemd timer/admin CLI, not a long-running daemon and not the MCP query path.
Tests can pass a fake client; the built-in REST client is a convenience for
manual smoke runs.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import http.client
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable, Protocol

from .core import (
    DEFAULT_PATCH_SNIPPET_BYTES,
    _truncate_bytes,
    init_db,
    record_indexer_run,
    upsert_pr_revision,
)

DEFAULT_MAX_RUNTIME_SECONDS = 45
DEFAULT_MAX_RSS_MB = 250
WATERMARK_SCHEMA_VERSION = 1
MAX_WATERMARK_PROCESSED_KEYS = 5000
TRANSIENT_GITHUB_ERRORS = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    urllib.error.URLError,
)


class PullRequestClient(Protocol):
    def iter_prs(
        self,
        repo: str,
        *,
        scope: str = "open",
        since: str | None = None,
        skip_keys: set[str] | None = None,
    ) -> Iterable[dict[str, Any]]:
        ...


class RequestBudgetExceeded(RuntimeError):
    """Raised when the one-shot indexer exhausts its upstream request budget."""


class TransientGitHubError(RuntimeError):
    """Raised after bounded retries for GitHub/network failures worth resuming."""


class RateLimitedGitHubError(RuntimeError):
    """Raised when GitHub asks the indexer to slow down."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GitHubNotFoundError(RuntimeError):
    """Raised for GitHub 404 responses that may be safe at leaf endpoints."""


class GitHubRestClient:
    """Tiny REST client for manual one-shot indexing."""

    def __init__(self, token: str | None = None, per_page: int = 50):
        self.token = token
        self.per_page = per_page
        self.requests_used = 0
        self.max_requests: int | None = None

    def set_request_budget(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self.requests_used = 0

    def _json(self, url: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pr-overlap-index/stdlib",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        last_error: BaseException | None = None
        for attempt in range(3):
            if self.max_requests is not None and self.requests_used >= self.max_requests:
                raise RequestBudgetExceeded("GitHub request budget exhausted")
            self.requests_used += 1
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code in {403, 429}:
                    retry_after = _retry_after_seconds(exc.headers)
                    raise RateLimitedGitHubError(
                        f"GitHub HTTP {exc.code}: {body[:300]}",
                        retry_after_seconds=retry_after,
                    ) from exc
                if exc.code == 404:
                    raise GitHubNotFoundError(
                        f"GitHub HTTP 404: {body[:300]}"
                    ) from exc
                if exc.code not in {502, 503, 504} or attempt >= 2:
                    raise RuntimeError(f"GitHub HTTP {exc.code}: {body[:300]}") from exc
                last_error = exc
            except TRANSIENT_GITHUB_ERRORS as exc:
                last_error = exc
                if attempt >= 2:
                    raise TransientGitHubError(
                        f"GitHub transient error after retries: {type(exc).__name__}: {exc}"
                    ) from exc
            time.sleep(1.0 + attempt)
        raise TransientGitHubError(f"GitHub transient error after retries: {last_error}")

    def iter_prs(
        self,
        repo: str,
        *,
        scope: str = "open",
        since: str | None = None,
        skip_keys: set[str] | None = None,
    ) -> Iterable[dict[str, Any]]:
        normalized_scope = normalize_scope(scope)
        state = "open" if normalized_scope == "hot_open" else "closed"
        page = 1
        while True:
            url = (
                f"https://api.github.com/repos/{repo}/pulls"
                f"?state={state}&sort=updated&direction=desc&per_page={self.per_page}&page={page}"
            )
            batch = self._json(url)
            if not batch:
                return
            for pr in batch:
                updated_at = str(pr.get("updated_at") or "")
                if since and updated_at <= since:
                    return
                key = _pr_resume_key({
                    "number": pr.get("number"),
                    "source_updated_at_raw": updated_at,
                    "head_sha": (pr.get("head") or {}).get("sha") or "",
                })
                if skip_keys and key in skip_keys:
                    continue
                files_url = (
                    f"https://api.github.com/repos/{repo}/pulls/{pr.get('number')}/files"
                )
                files_error: dict[str, str] | None = None
                try:
                    files = self._json(files_url) if pr.get("number") else []
                except GitHubNotFoundError as exc:
                    files = []
                    files_error = {
                        "type": type(exc).__name__,
                        "detail": str(exc),
                        "url": files_url,
                    }
                yield {
                    "number": pr.get("number"),
                    "title": pr.get("title") or "",
                    "body": pr.get("body") or "",
                    "state": pr.get("state") or state,
                    "head_sha": (pr.get("head") or {}).get("sha") or "",
                    "base_sha": (pr.get("base") or {}).get("sha") or "",
                    "source_updated_at": parse_github_timestamp(updated_at),
                    "source_updated_at_raw": updated_at,
                    "url": pr.get("html_url") or "",
                    "files_error": files_error,
                    "files": [
                        {
                            "path": f.get("filename") or "",
                            "status": f.get("status") or "modified",
                            "additions": f.get("additions") or 0,
                            "deletions": f.get("deletions") or 0,
                            "patch": f.get("patch") or "",
                        }
                        for f in files
                    ],
                }
            page += 1


def normalize_scope(scope: str) -> str:
    """Map legacy cron/CLI scope names to explicit index profiles."""
    aliases = {
        "open": "hot_open",
        "hot": "hot_open",
        "hot_open": "hot_open",
        "recent": "recent_closed",
        "recent_closed": "recent_closed",
        "closed": "recent_closed",
        "cold": "cold_archive",
        "cold_archive": "cold_archive",
        "targeted": "targeted_refresh",
        "targeted_refresh": "targeted_refresh",
    }
    return aliases.get(scope, scope)


def parse_github_timestamp(value: str | None) -> float:
    """Parse a GitHub ISO timestamp as epoch seconds.

    Returns 0 for missing values so callers can distinguish unknown upstream
    freshness from local capture time.
    """
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _retry_after_seconds(headers: Any) -> int | None:
    value = None
    if headers is not None:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            value = None
    if value in (None, ""):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _load_watermark(db: str | Path, repo: str, scope: str) -> dict[str, Any]:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT watermark FROM indexer_watermarks WHERE repo=? AND scope=?",
            (repo, scope),
        ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            value = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return {"since": str(row[0])}
        return value if isinstance(value, dict) else {}
    finally:
        conn.close()


def _save_watermark(
    db: str | Path,
    repo: str,
    scope: str,
    watermark: dict[str, Any],
) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """INSERT INTO indexer_watermarks(repo,scope,watermark,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(repo,scope) DO UPDATE SET
                 watermark=excluded.watermark,
                 updated_at=excluded.updated_at""",
            (repo, scope, json.dumps(watermark, sort_keys=True), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _pr_resume_key(pr: dict[str, Any]) -> str:
    source = pr.get("source_updated_at_raw")
    if source in (None, ""):
        source = pr.get("source_updated_at")
    if source in (None, ""):
        source = pr.get("head_sha", "")
    return f"{pr.get('number')}:{source}"


def _pr_watermark_value(pr: dict[str, Any]) -> str | None:
    value = pr.get("source_updated_at_raw")
    if value not in (None, ""):
        return str(value)
    value = pr.get("source_updated_at")
    if value not in (None, ""):
        return str(value)
    return None


def _watermark_sort_key(value: str) -> tuple[int, float | str]:
    try:
        return (1, float(value))
    except (TypeError, ValueError):
        return (2, value)


def _max_watermark(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    return max(current, candidate, key=_watermark_sort_key)


def _bounded_processed_keys(keys: set[str]) -> list[str]:
    return sorted(keys)[-MAX_WATERMARK_PROCESSED_KEYS:]


def _rss_mb() -> float:
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform == "darwin":
            return rss / (1024 * 1024)
        return rss / 1024
    except (ImportError, OSError, ValueError):
        return 0.0


def _default_probe(db: str | Path) -> dict[str, float]:
    parent = Path(db).expanduser().resolve().parent
    usage = shutil.disk_usage(parent)
    return {"rss_mb": _rss_mb(), "free_disk_bytes": float(usage.free)}


def _import_lock_active(db: str | Path, *, stale_after_seconds: int = 900) -> bool:
    lock_path = Path(f"{Path(db)}.import.lock")
    _ = stale_after_seconds
    return lock_path.exists()


def _acquire_lock(
    db: str | Path, *, name: str, holder: str, stale_after_seconds: int = 900
) -> tuple[bool, sqlite3.Connection]:
    now = time.time()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "DELETE FROM indexer_locks WHERE heartbeat_at<?",
        (now - stale_after_seconds,),
    )
    active_import = conn.execute(
        "SELECT name FROM indexer_locks WHERE name LIKE 'import:%' LIMIT 1"
    ).fetchone()
    if active_import:
        conn.commit()
        return False, conn
    try:
        conn.execute(
            "INSERT INTO indexer_locks(name,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?)",
            (name, holder, now, now),
        )
        conn.commit()
        return True, conn
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT holder,heartbeat_at FROM indexer_locks WHERE name=?", (name,)
        ).fetchone()
        if row and now - float(row[1]) > stale_after_seconds:
            conn.execute(
                "UPDATE indexer_locks SET holder=?,acquired_at=?,heartbeat_at=? WHERE name=?",
                (holder, now, now, name),
            )
            conn.commit()
            return True, conn
        return False, conn


def _release_lock(conn: sqlite3.Connection, *, name: str, holder: str) -> None:
    conn.execute("DELETE FROM indexer_locks WHERE name=? AND holder=?", (name, holder))
    conn.commit()
    conn.close()


def _cap_files(
    files: list[dict[str, Any]],
    *,
    max_files_per_pr: int,
    max_patch_bytes: int,
    max_total_patch_bytes: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    total = 0
    truncated_files = 0
    skipped_files = max(0, len(files) - max_files_per_pr)
    for raw in files[:max_files_per_pr]:
        patch = str(raw.get("patch_snippet", raw.get("patch", "")))
        snippet = _truncate_bytes(patch, max_patch_bytes)
        if len(patch.encode("utf-8")) > len(snippet.encode("utf-8")):
            truncated_files += 1
        encoded_len = len(snippet.encode("utf-8"))
        if total + encoded_len > max_total_patch_bytes:
            skipped_files += 1
            continue
        total += encoded_len
        item = dict(raw)
        item["patch_snippet"] = snippet
        item.pop("patch", None)
        capped.append(item)
    return capped, {
        "stored_files": len(capped),
        "skipped_files": skipped_files,
        "truncated_files": truncated_files,
        "stored_patch_bytes": total,
    }


def run_one_shot_indexer(
    db: str | Path,
    repo: str,
    *,
    client: PullRequestClient,
    scope: str = "open",
    since: str | None = None,
    max_requests: int = 80,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    max_rss_mb: int = DEFAULT_MAX_RSS_MB,
    min_free_disk_bytes: int = 4 * 1024 * 1024 * 1024,
    batch_limit: int = 300,
    max_files_per_pr: int = 30,
    max_patch_bytes: int = DEFAULT_PATCH_SNIPPET_BYTES,
    max_total_patch_bytes: int = 256 * 1024,
    resource_probe: Any | None = None,
) -> dict[str, Any]:
    """Run one bounded indexing pass and return a machine-readable summary."""
    start = time.monotonic()
    started_at = time.time()
    normalized_scope = normalize_scope(scope)
    if _import_lock_active(db):
        return {
            "repo": repo,
            "scope": normalized_scope,
            "input_scope": scope,
            "profile": normalized_scope,
            "holder": None,
            "indexed": 0,
            "requests_used": 0,
            "capped": True,
            "stop_reason": "import_lock_held",
            "skipped_files": 0,
            "truncated_files": 0,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "effective_since": since,
            "resume_hint": {
                "processed_keys": 0,
                "max_seen": None,
            },
            "run_id": None,
            "db_bytes": None,
            "wal_bytes": None,
            "free_disk_bytes": None,
        }
    init_db(db)
    probe = resource_probe or _default_probe
    stored_watermark = _load_watermark(db, repo, normalized_scope)
    effective_since = since if since is not None else stored_watermark.get("since")
    processed_keys = set()
    if since is None:
        processed_keys = {
            str(key) for key in stored_watermark.get("processed_keys", [])
        }
    max_seen_watermark = stored_watermark.get("max_seen")
    holder = uuid.uuid4().hex
    locked, lock_conn = _acquire_lock(db, name=f"indexer:{repo}", holder=holder)
    if not locked:
        lock_conn.close()
        finished_at = time.time()
        run_record = record_indexer_run(
            db,
            repo,
            normalized_scope,
            profile=normalized_scope,
            started_at=started_at,
            finished_at=finished_at,
            indexed=0,
            requests_used=0,
            capped=True,
            stop_reason="lock_held",
            max_requests=max_requests,
            max_runtime_seconds=max_runtime_seconds,
            max_rss_mb=max_rss_mb,
            min_free_disk_bytes=min_free_disk_bytes,
            batch_limit=batch_limit,
            max_files_per_pr=max_files_per_pr,
            max_patch_bytes=max_patch_bytes,
            detail={"input_scope": scope, "since": since},
        )
        return {
            "repo": repo,
            "scope": normalized_scope,
            "input_scope": scope,
            "profile": normalized_scope,
            "holder": holder,
            "indexed": 0,
            "requests_used": 0,
            "capped": True,
            "stop_reason": "lock_held",
            "skipped_files": 0,
            "truncated_files": 0,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "effective_since": effective_since,
            "resume_hint": {
                "processed_keys": len(processed_keys),
                "max_seen": max_seen_watermark,
            },
            "run_id": run_record["id"],
            "db_bytes": run_record.get("db_bytes"),
            "wal_bytes": run_record.get("wal_bytes"),
            "free_disk_bytes": run_record.get("free_disk_bytes"),
        }
    indexed = 0
    requests_used = 0
    skipped_files = 0
    truncated_files = 0
    file_fetch_errors = 0
    stop_reason = "complete"
    capped = False
    error_summary: dict[str, Any] | None = None

    if hasattr(client, "set_request_budget"):
        client.set_request_budget(max_requests)  # type: ignore[attr-defined]
    try:
        try:
            pr_iter = client.iter_prs(
                repo,
                scope=normalized_scope,
                since=effective_since,
                skip_keys=processed_keys,
            )
        except TypeError:
            pr_iter = client.iter_prs(
                repo, scope=normalized_scope, since=effective_since
            )
        for pr in pr_iter:
            resume_key = _pr_resume_key(pr)
            if resume_key in processed_keys:
                continue
            elapsed = time.monotonic() - start
            sample = probe(db)
            if elapsed >= max_runtime_seconds:
                stop_reason = "runtime_cap"
                capped = True
                break
            client_requests = int(getattr(client, "requests_used", requests_used))
            if client_requests >= max_requests:
                stop_reason = "request_cap"
                capped = True
                break
            if float(sample.get("rss_mb", 0)) >= max_rss_mb:
                stop_reason = "rss_cap"
                capped = True
                break
            if float(sample.get("free_disk_bytes", min_free_disk_bytes)) < min_free_disk_bytes:
                stop_reason = "disk_cap"
                capped = True
                break
            if indexed >= batch_limit:
                stop_reason = "batch_cap"
                capped = True
                break

            if not hasattr(client, "requests_used"):
                requests_used += 1
            if pr.get("files_error"):
                file_fetch_errors += 1
            files, stats = _cap_files(
                list(pr.get("files") or []),
                max_files_per_pr=max_files_per_pr,
                max_patch_bytes=max_patch_bytes,
                max_total_patch_bytes=max_total_patch_bytes,
            )
            skipped_files += int(stats["skipped_files"])
            truncated_files += int(stats["truncated_files"])
            upsert_pr_revision(
                db,
                repo,
                int(pr["number"]),
                title=str(pr.get("title") or ""),
                body=str(pr.get("body") or ""),
                state=str(pr.get("state") or scope),
                files=files,
                head_sha=str(pr.get("head_sha") or ""),
                base_sha=str(pr.get("base_sha") or ""),
                source_updated_at=float(pr.get("source_updated_at") or 0),
                url=str(pr.get("url") or ""),
                index_scope=normalized_scope,
            )
            processed_keys.add(resume_key)
            max_seen_watermark = _max_watermark(
                max_seen_watermark, _pr_watermark_value(pr)
            )
            _save_watermark(
                db,
                repo,
                normalized_scope,
                {
                    "schema_version": WATERMARK_SCHEMA_VERSION,
                    "since": effective_since,
                    "processed_keys": _bounded_processed_keys(processed_keys),
                    "max_seen": max_seen_watermark,
                    "last_processed_key": resume_key,
                    "stop_reason": "running",
                    "capped": True,
                },
            )
            lock_conn.execute(
                "UPDATE indexer_locks SET heartbeat_at=? WHERE name=? AND holder=?",
                (time.time(), f"indexer:{repo}", holder),
            )
            lock_conn.commit()
            indexed += 1
    except RequestBudgetExceeded:
        stop_reason = "request_cap"
        capped = True
    except RateLimitedGitHubError as exc:
        stop_reason = "rate_limited"
        capped = True
        error_summary = {
            "type": type(exc).__name__,
            "detail": str(exc),
            "retry_after_seconds": exc.retry_after_seconds,
        }
    except TransientGitHubError as exc:
        stop_reason = "transient_network"
        capped = True
        error_summary = {
            "type": type(exc).__name__,
            "detail": str(exc),
        }
    finally:
        _release_lock(lock_conn, name=f"indexer:{repo}", holder=holder)
    requests_used = int(getattr(client, "requests_used", requests_used))
    finished_at = time.time()
    final_since = effective_since
    final_processed_keys = _bounded_processed_keys(processed_keys)
    if not capped:
        final_since = max_seen_watermark or effective_since
        final_processed_keys = []
    final_watermark = {
        "schema_version": WATERMARK_SCHEMA_VERSION,
        "since": final_since,
        "processed_keys": final_processed_keys,
        "max_seen": max_seen_watermark,
        "stop_reason": stop_reason,
        "capped": capped,
        "last_finished_at": finished_at,
    }
    _save_watermark(db, repo, normalized_scope, final_watermark)
    run_record = record_indexer_run(
        db,
        repo,
        normalized_scope,
        profile=normalized_scope,
        started_at=started_at,
        finished_at=finished_at,
        indexed=indexed,
        requests_used=requests_used,
        capped=capped,
        stop_reason=stop_reason,
        max_requests=max_requests,
        max_runtime_seconds=max_runtime_seconds,
        max_rss_mb=max_rss_mb,
        min_free_disk_bytes=min_free_disk_bytes,
        batch_limit=batch_limit,
        max_files_per_pr=max_files_per_pr,
        max_patch_bytes=max_patch_bytes,
        detail={
            "input_scope": scope,
            "since": since,
            "effective_since": effective_since,
            "watermark": final_watermark,
            "skipped_files": skipped_files,
            "truncated_files": truncated_files,
            "file_fetch_errors": file_fetch_errors,
            "error": error_summary,
        },
    )

    return {
        "repo": repo,
        "scope": normalized_scope,
        "input_scope": scope,
        "profile": normalized_scope,
        "holder": holder,
        "indexed": indexed,
        "requests_used": requests_used,
        "capped": capped,
        "stop_reason": stop_reason,
        "skipped_files": skipped_files,
        "truncated_files": truncated_files,
        "file_fetch_errors": file_fetch_errors,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "effective_since": effective_since,
        "resume_hint": {
            "processed_keys": len(final_processed_keys),
            "max_seen": max_seen_watermark,
            "since": final_since,
        },
        "run_id": run_record["id"],
        "db_bytes": run_record.get("db_bytes"),
        "wal_bytes": run_record.get("wal_bytes"),
        "free_disk_bytes": run_record.get("free_disk_bytes"),
        "error": error_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded PR index pass.")
    parser.add_argument("repo", help="owner/name repository")
    parser.add_argument("--db", default=os.environ.get("PR_OVERLAP_DB", "pr-overlap.db"))
    parser.add_argument(
        "--scope",
        default="open",
        choices=(
            "open",
            "recent",
            "closed",
            "hot_open",
            "recent_closed",
            "cold_archive",
            "targeted_refresh",
        ),
    )
    parser.add_argument("--since")
    parser.add_argument("--max-requests", type=int, default=80)
    parser.add_argument("--max-runtime-seconds", type=int, default=DEFAULT_MAX_RUNTIME_SECONDS)
    parser.add_argument("--max-rss-mb", type=int, default=DEFAULT_MAX_RSS_MB)
    parser.add_argument("--min-free-disk-gb", type=float, default=4.0)
    parser.add_argument("--batch-limit", type=int, default=300)
    parser.add_argument("--max-files-per-pr", type=int, default=30)
    parser.add_argument("--max-patch-bytes", type=int, default=DEFAULT_PATCH_SNIPPET_BYTES)
    args = parser.parse_args(argv)
    client = GitHubRestClient(os.environ.get("GITHUB_TOKEN"))
    summary = run_one_shot_indexer(
        args.db,
        args.repo,
        client=client,
        scope=args.scope,
        since=args.since,
        max_requests=args.max_requests,
        max_runtime_seconds=args.max_runtime_seconds,
        max_rss_mb=args.max_rss_mb,
        min_free_disk_bytes=int(args.min_free_disk_gb * 1024 * 1024 * 1024),
        batch_limit=args.batch_limit,
        max_files_per_pr=args.max_files_per_pr,
        max_patch_bytes=args.max_patch_bytes,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
