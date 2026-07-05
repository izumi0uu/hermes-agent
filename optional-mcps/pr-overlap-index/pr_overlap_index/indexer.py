"""Bounded one-shot GitHub PR indexer.

This module is intentionally small and stdlib-only.  It is designed for a
systemd timer/admin CLI, not a long-running daemon and not the MCP query path.
Tests can pass a fake client; the built-in REST client is a convenience for
manual smoke runs.
"""

from __future__ import annotations

import argparse
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
    upsert_pr_revision,
)


class PullRequestClient(Protocol):
    def iter_prs(
        self, repo: str, *, scope: str = "open", since: str | None = None
    ) -> Iterable[dict[str, Any]]:
        ...


class RequestBudgetExceeded(RuntimeError):
    """Raised when the one-shot indexer exhausts its upstream request budget."""


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
        if self.max_requests is not None and self.requests_used >= self.max_requests:
            raise RequestBudgetExceeded("GitHub request budget exhausted")
        self.requests_used += 1
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pr-overlap-index/stdlib",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"GitHub HTTP {exc.code}: {body[:300]}") from exc

    def iter_prs(
        self, repo: str, *, scope: str = "open", since: str | None = None
    ) -> Iterable[dict[str, Any]]:
        state = "open" if scope == "open" else "closed"
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
                files_url = str(pr.get("url", "")) + "/files"
                files = self._json(files_url) if pr.get("url") else []
                yield {
                    "number": pr.get("number"),
                    "title": pr.get("title") or "",
                    "body": pr.get("body") or "",
                    "state": pr.get("state") or state,
                    "head_sha": (pr.get("head") or {}).get("sha") or "",
                    "base_sha": (pr.get("base") or {}).get("sha") or "",
                    "source_updated_at": time.time(),
                    "url": pr.get("html_url") or "",
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


def _rss_mb() -> float:
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform == "darwin":
            return rss / (1024 * 1024)
        return rss / 1024
    except Exception:
        return 0.0


def _default_probe(db: str | Path) -> dict[str, float]:
    parent = Path(db).expanduser().resolve().parent
    usage = shutil.disk_usage(parent)
    return {"rss_mb": _rss_mb(), "free_disk_bytes": float(usage.free)}


def _acquire_lock(
    db: str | Path, *, name: str, holder: str, stale_after_seconds: int = 900
) -> tuple[bool, sqlite3.Connection]:
    conn = sqlite3.connect(str(db))
    now = time.time()
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
    max_runtime_seconds: int = 120,
    max_rss_mb: int = 350,
    min_free_disk_bytes: int = 4 * 1024 * 1024 * 1024,
    batch_limit: int = 300,
    max_files_per_pr: int = 30,
    max_patch_bytes: int = DEFAULT_PATCH_SNIPPET_BYTES,
    max_total_patch_bytes: int = 256 * 1024,
    resource_probe: Any | None = None,
) -> dict[str, Any]:
    """Run one bounded indexing pass and return a machine-readable summary."""
    init_db(db)
    start = time.monotonic()
    probe = resource_probe or _default_probe
    holder = uuid.uuid4().hex
    locked, lock_conn = _acquire_lock(db, name=f"indexer:{repo}", holder=holder)
    if not locked:
        lock_conn.close()
        return {
            "repo": repo,
            "scope": scope,
            "holder": holder,
            "indexed": 0,
            "requests_used": 0,
            "capped": True,
            "stop_reason": "lock_held",
            "skipped_files": 0,
            "truncated_files": 0,
            "elapsed_seconds": round(time.monotonic() - start, 3),
        }
    indexed = 0
    requests_used = 0
    skipped_files = 0
    truncated_files = 0
    stop_reason = "complete"
    capped = False

    if hasattr(client, "set_request_budget"):
        client.set_request_budget(max_requests)  # type: ignore[attr-defined]
    try:
        for pr in client.iter_prs(repo, scope=scope, since=since):
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
                source_updated_at=float(pr.get("source_updated_at") or time.time()),
                url=str(pr.get("url") or ""),
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
    finally:
        _release_lock(lock_conn, name=f"indexer:{repo}", holder=holder)
    requests_used = int(getattr(client, "requests_used", requests_used))

    return {
        "repo": repo,
        "scope": scope,
        "holder": holder,
        "indexed": indexed,
        "requests_used": requests_used,
        "capped": capped,
        "stop_reason": stop_reason,
        "skipped_files": skipped_files,
        "truncated_files": truncated_files,
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded PR index pass.")
    parser.add_argument("repo", help="owner/name repository")
    parser.add_argument("--db", default=os.environ.get("PR_OVERLAP_DB", "pr-overlap.db"))
    parser.add_argument("--scope", default="open", choices=("open", "recent", "closed"))
    parser.add_argument("--since")
    parser.add_argument("--max-requests", type=int, default=80)
    parser.add_argument("--max-runtime-seconds", type=int, default=120)
    parser.add_argument("--max-rss-mb", type=int, default=350)
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
