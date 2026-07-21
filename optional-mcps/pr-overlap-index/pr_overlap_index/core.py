"""Small stdlib-only PR overlap index.

The module intentionally keeps V1 compact: SQLite stores immutable revision
manifests plus a mutable latest pointer; searches are local-only and archive
class (`full-cover`) answers require a live matching refresh lease and a replay
capsule.  No network or GitHub client code lives here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3
BUNDLE_SCHEMA_VERSION = 1
SCORE_VERSION = "lexical-v1"
MATCHER_VERSION = "compact-v1"
NORMALIZATION_VERSION = "text-v1"
DEFAULT_PATCH_SNIPPET_BYTES = 8192
INDEX_VERSION = {
    "score": SCORE_VERSION,
    "matcher": MATCHER_VERSION,
    "normalization": NORMALIZATION_VERSION,
}


class IndexNotInitializedError(RuntimeError):
    """Raised when a read/query path is pointed at a missing index."""


def _configure_connection(conn: sqlite3.Connection, *, write: bool = True) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if write:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Some test/in-memory connections cannot switch journal modes.
            pass
        conn.execute("PRAGMA synchronous=NORMAL")


def _connect(db: str | Path | sqlite3.Connection) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db, sqlite3.Connection):
        _configure_connection(db)
        return db, False
    conn = sqlite3.connect(str(db))
    _configure_connection(conn)
    return conn, True


def open_existing_index_db(
    db: str | Path | sqlite3.Connection, *, validate_schema: bool = True
) -> sqlite3.Connection:
    """Open an existing index without creating files or running migrations."""
    if isinstance(db, sqlite3.Connection):
        _configure_connection(db, write=False)
        conn = db
    else:
        path = Path(db)
        if not path.exists():
            raise IndexNotInitializedError(f"PR overlap index is not initialized: {path}")
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
        _configure_connection(conn, write=False)
    if validate_schema:
        _validate_schema(conn)
    return conn


def _connect_existing(
    db: str | Path | sqlite3.Connection, *, validate_schema: bool = True
) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db, sqlite3.Connection):
        return open_existing_index_db(db, validate_schema=validate_schema), False
    return open_existing_index_db(db, validate_schema=validate_schema), True


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    return row[0]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _validate_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "meta"):
        raise IndexNotInitializedError("PR overlap index schema is missing meta table")
    schema_version = _scalar(conn, "SELECT value FROM meta WHERE key='schema_version'")
    if schema_version is None:
        raise IndexNotInitializedError("PR overlap index schema version is missing")
    if int(schema_version) != SCHEMA_VERSION:
        raise IndexNotInitializedError(
            f"PR overlap index schema version {schema_version} != expected {SCHEMA_VERSION}"
        )


def _truncate_bytes(text: str, max_bytes: int = DEFAULT_PATCH_SNIPPET_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def _terms(values: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            out.update(_terms(value))
            continue
        text = str(value).lower()
        for token in re.findall(r"[a-z0-9_./:-]{3,}", text):
            out.add(token)
    return out


def _scope_profile(scope: str) -> str:
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


def _repo_parts(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        return "", repo
    owner, name = repo.split("/", 1)
    return owner, name


def _ensure_repo(conn: sqlite3.Connection, repo: str) -> int:
    owner, name = _repo_parts(repo)
    conn.execute("INSERT OR IGNORE INTO repos(owner,name) VALUES(?,?)", (owner, name))
    return int(
        conn.execute(
            "SELECT id FROM repos WHERE owner=? AND name=?", (owner, name)
        ).fetchone()[0]
    )


def _manifest_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def init_db(db: str | Path | sqlite3.Connection) -> None:
    """Create or migrate the compact PR overlap schema."""
    conn, close = _connect(db)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS repos(
              id INTEGER PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
              UNIQUE(owner,name)
            );
            CREATE TABLE IF NOT EXISTS prs(
              repo_id INTEGER NOT NULL, number INTEGER NOT NULL, state TEXT DEFAULT 'open',
              title TEXT DEFAULT '', body TEXT DEFAULT '', url TEXT DEFAULT '',
              head_sha TEXT DEFAULT '', base_sha TEXT DEFAULT '', source_updated_at REAL DEFAULT 0,
              latest_manifest_id INTEGER, last_refreshed_at REAL DEFAULT 0,
              evidence_version INTEGER DEFAULT 1,
              PRIMARY KEY(repo_id, number)
            );
            CREATE TABLE IF NOT EXISTS pr_revision_manifests(
              id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, pr_number INTEGER NOT NULL,
              head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, captured_at REAL NOT NULL,
              source_updated_at REAL NOT NULL, evidence_version INTEGER NOT NULL,
              body_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL UNIQUE,
              parent_manifest_id INTEGER, superseded_by_manifest_id INTEGER,
              tombstoned_at REAL, tombstone_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS pr_revision_hunks(
              id INTEGER PRIMARY KEY, manifest_id INTEGER NOT NULL, path TEXT NOT NULL,
              patch_hash TEXT DEFAULT '', hunk_hash TEXT DEFAULT '',
              patch_snippet TEXT DEFAULT '', diff_terms TEXT DEFAULT '', status TEXT DEFAULT 'modified',
              additions INTEGER DEFAULT 0, deletions INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS refresh_leases(
              lease_id TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, pr_number INTEGER NOT NULL,
              head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, evidence_version INTEGER NOT NULL,
              issued_at REAL NOT NULL, expires_at REAL NOT NULL, refresh_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refresh_dead_letters(
              id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, pr_number INTEGER NOT NULL,
              reason_class TEXT NOT NULL, first_failed_at REAL NOT NULL, last_failed_at REAL NOT NULL,
              attempt_count INTEGER NOT NULL, next_retry_at REAL, last_error_summary TEXT DEFAULT '',
              terminal INTEGER DEFAULT 0,
              UNIQUE(repo_id, pr_number, reason_class)
            );
            CREATE TABLE IF NOT EXISTS replay_capsules(
              capsule_id TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, issue_number INTEGER,
              pr_number INTEGER NOT NULL, classification TEXT NOT NULL, capsule_hash TEXT NOT NULL,
              payload_json TEXT NOT NULL, emitted_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS indexer_locks(
              name TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at REAL NOT NULL,
              heartbeat_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS indexer_watermarks(
              repo TEXT NOT NULL, scope TEXT NOT NULL, watermark TEXT DEFAULT '',
              updated_at REAL NOT NULL, PRIMARY KEY(repo, scope)
            );
            CREATE TABLE IF NOT EXISTS resource_samples(
              id INTEGER PRIMARY KEY, sampled_at REAL NOT NULL, metric TEXT NOT NULL,
              value REAL NOT NULL, detail TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pr_scope_membership(
              repo_id INTEGER NOT NULL, pr_number INTEGER NOT NULL, scope TEXT NOT NULL,
              first_indexed_at REAL NOT NULL, last_indexed_at REAL NOT NULL,
              source_updated_at REAL DEFAULT 0,
              PRIMARY KEY(repo_id, pr_number, scope)
            );
            CREATE TABLE IF NOT EXISTS pr_revision_terms(
              manifest_id INTEGER NOT NULL, term TEXT NOT NULL,
              PRIMARY KEY(manifest_id, term)
            );
            CREATE INDEX IF NOT EXISTS idx_pr_revision_terms_term
              ON pr_revision_terms(term, manifest_id);
            CREATE TABLE IF NOT EXISTS indexer_runs(
              id INTEGER PRIMARY KEY, repo TEXT NOT NULL, scope TEXT NOT NULL,
              profile TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL NOT NULL,
              indexed INTEGER NOT NULL, requests_used INTEGER NOT NULL,
              capped INTEGER NOT NULL, stop_reason TEXT NOT NULL,
              max_requests INTEGER, max_runtime_seconds INTEGER, max_rss_mb INTEGER,
              min_free_disk_bytes INTEGER, batch_limit INTEGER, max_files_per_pr INTEGER,
              max_patch_bytes INTEGER, db_bytes INTEGER DEFAULT 0,
              wal_bytes INTEGER DEFAULT 0, free_disk_bytes INTEGER DEFAULT 0,
              detail_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_indexer_runs_repo_scope_finished
              ON indexer_runs(repo, scope, finished_at DESC);
            """
        )
        if "patch_hash" not in _columns(conn, "pr_revision_hunks"):
            conn.execute(
                "ALTER TABLE pr_revision_hunks ADD COLUMN patch_hash TEXT DEFAULT ''"
            )
        if "hunk_hash" not in _columns(conn, "pr_revision_hunks"):
            conn.execute(
                "ALTER TABLE pr_revision_hunks ADD COLUMN hunk_hash TEXT DEFAULT ''"
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def health_snapshot(db: str | Path | sqlite3.Connection) -> dict[str, Any]:
    """Return a stdlib-only health snapshot for the optional overlap index."""
    try:
        conn, close = _connect_existing(db, validate_schema=True)
    except IndexNotInitializedError as exc:
        return {
            "ok": False,
            "initialized": False,
            "error": str(exc),
            "schema_version": None,
            "expected_schema_version": SCHEMA_VERSION,
            "index_version": INDEX_VERSION,
        }
    try:
        now = _now()
        schema_version = _scalar(
            conn, "SELECT value FROM meta WHERE key='schema_version'"
        )
        latest_refresh = _scalar(conn, "SELECT MAX(last_refreshed_at) FROM prs")
        latest_source_update = _scalar(conn, "SELECT MAX(source_updated_at) FROM prs")
        latest_lease = _scalar(conn, "SELECT MAX(issued_at) FROM refresh_leases")
        latest_dlq = _scalar(
            conn, "SELECT MAX(last_failed_at) FROM refresh_dead_letters"
        )
        capsule_count = int(_scalar(conn, "SELECT COUNT(*) FROM replay_capsules") or 0)
        capsule_bytes = int(
            _scalar(
                conn,
                "SELECT COALESCE(SUM(LENGTH(payload_json)),0) FROM replay_capsules",
            )
            or 0
        )
        timestamps = [
            ts
            for ts in (latest_refresh, latest_source_update, latest_lease, latest_dlq)
            if ts
        ]
        coverage = _coverage_by_scope(conn)
        last_runs = _last_runs_by_scope(conn)
        sizes = _db_size_snapshot(db)
        return {
            "ok": True,
            "initialized": True,
            "schema_version": int(schema_version or SCHEMA_VERSION),
            "index_version": INDEX_VERSION,
            "indexed_pr_count": int(_scalar(conn, "SELECT COUNT(*) FROM prs") or 0),
            "latest_live_manifest_count": int(
                _scalar(
                    conn,
                    "SELECT COUNT(DISTINCT latest_manifest_id) FROM prs WHERE latest_manifest_id IS NOT NULL",
                )
                or 0
            ),
            "tombstone_count": int(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pr_revision_manifests WHERE tombstoned_at IS NOT NULL",
                )
                or 0
            ),
            "active_lease_count": int(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM refresh_leases WHERE expires_at>?",
                    (now,),
                )
                or 0
            ),
            "dead_letter_count": int(
                _scalar(conn, "SELECT COUNT(*) FROM refresh_dead_letters") or 0
            ),
            "historical_anchor_count": int(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pr_revision_manifests WHERE superseded_by_manifest_id IS NOT NULL OR tombstoned_at IS NOT NULL",
                )
                or 0
            ),
            "replay_capsule_count": capsule_count,
            "replay_capsule_bytes": capsule_bytes,
            "replay_capsule_oldest_at": _scalar(
                conn, "SELECT MIN(emitted_at) FROM replay_capsules"
            ),
            "replay_capsule_newest_at": _scalar(
                conn, "SELECT MAX(emitted_at) FROM replay_capsules"
            ),
            "last_refreshed_at": latest_refresh,
            "last_source_updated_at": latest_source_update,
            "last_sync_at": max(timestamps) if timestamps else None,
            "coverage_by_scope": coverage,
            "last_run_by_scope": last_runs,
            **sizes,
            "brownout_state": _brownout_state(sizes),
            "archive_confidence_floor": _archive_confidence_floor(coverage),
        }
    finally:
        if close:
            conn.close()


def _db_family_paths(db: str | Path) -> list[Path]:
    path = Path(db)
    return [path, Path(f"{path}-wal"), Path(f"{path}-shm")]


def _import_lock_path(db: str | Path) -> Path:
    return Path(f"{Path(db)}.import.lock")


def _import_lockfile_exists(db: str | Path | sqlite3.Connection) -> bool:
    if isinstance(db, sqlite3.Connection):
        try:
            for row in db.execute("PRAGMA database_list"):
                name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
                path = row["file"] if isinstance(row, sqlite3.Row) else row[2]
                if name == "main" and path:
                    return _import_lock_path(path).exists()
        except sqlite3.Error:
            return False
        return False
    return _import_lock_path(db).exists()


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _db_size_snapshot(db: str | Path | sqlite3.Connection) -> dict[str, Any]:
    if isinstance(db, sqlite3.Connection):
        return {
            "db_bytes": None,
            "wal_bytes": None,
            "shm_bytes": None,
            "free_disk_bytes": None,
        }
    path = Path(db)
    parent = path.expanduser().resolve().parent
    try:
        stat = os.statvfs(parent)
        free_disk_bytes = int(stat.f_bavail * stat.f_frsize)
    except OSError:
        free_disk_bytes = None
    return {
        "db_bytes": _path_size(path),
        "wal_bytes": _path_size(Path(f"{path}-wal")),
        "shm_bytes": _path_size(Path(f"{path}-shm")),
        "free_disk_bytes": free_disk_bytes,
    }


def _brownout_state(sizes: dict[str, Any], min_free_disk_bytes: int = 4 * 1024 * 1024 * 1024) -> str:
    free = sizes.get("free_disk_bytes")
    if free is None:
        return "unknown"
    return "disk_brownout" if int(free) < min_free_disk_bytes else "ok"


def _coverage_by_scope(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute(
        """SELECT scope, COUNT(*) AS coverage_count,
                  MIN(source_updated_at) AS oldest_source_updated_at,
                  MAX(source_updated_at) AS newest_source_updated_at,
                  MAX(last_indexed_at) AS coverage_as_of
           FROM pr_scope_membership GROUP BY scope"""
    ):
        scope = str(row["scope"])
        oldest = row["oldest_source_updated_at"]
        newest = row["newest_source_updated_at"]
        out[scope] = {
            "coverage_count": int(row["coverage_count"] or 0),
            "coverage_window": {
                "oldest_source_updated_at": oldest,
                "newest_source_updated_at": newest,
            },
            "coverage_as_of": row["coverage_as_of"],
            "coverage_denominator_kind": "unknown",
            "coverage_denominator": None,
            "coverage_source": scope,
            "serving_active": scope != "cold_archive",
            "serving_gate": (
                "passed" if scope != "cold_archive" else "cold_scale_gate_required"
            ),
        }
    if not out:
        count = int(_scalar(conn, "SELECT COUNT(*) FROM prs") or 0)
        newest = _scalar(conn, "SELECT MAX(source_updated_at) FROM prs")
        oldest = _scalar(conn, "SELECT MIN(source_updated_at) FROM prs")
        if count:
            out["legacy"] = {
                "coverage_count": count,
                "coverage_window": {
                    "oldest_source_updated_at": oldest,
                    "newest_source_updated_at": newest,
                },
                "coverage_as_of": newest,
                "coverage_denominator_kind": "unknown",
                "coverage_denominator": None,
                "coverage_source": "legacy",
                "serving_active": True,
                "serving_gate": "legacy",
            }
    return out


def _last_runs_by_scope(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute(
        """SELECT * FROM indexer_runs r
           WHERE id IN (
             SELECT MAX(id) FROM indexer_runs GROUP BY repo, scope
           )
           ORDER BY finished_at DESC"""
    ):
        out[str(row["scope"])] = {
            "repo": row["repo"],
            "profile": row["profile"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "indexed": int(row["indexed"]),
            "requests_used": int(row["requests_used"]),
            "capped": bool(row["capped"]),
            "stop_reason": row["stop_reason"],
            "db_bytes": int(row["db_bytes"] or 0),
            "wal_bytes": int(row["wal_bytes"] or 0),
            "free_disk_bytes": int(row["free_disk_bytes"] or 0),
        }
    return out


def _archive_confidence_floor(coverage: dict[str, Any]) -> str:
    if coverage.get("cold_archive", {}).get("serving_active"):
        return "hot_plus_cold"
    if "recent_closed" in coverage:
        return "hot_plus_recent"
    if "hot_open" in coverage:
        return "hot_only"
    return "unknown"


def _upsert_pr_revision_conn(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    title: str = "",
    body: str = "",
    state: str = "open",
    files: list[dict[str, Any]] | None = None,
    head_sha: str = "",
    base_sha: str = "",
    source_updated_at: float | None = None,
    url: str = "",
    evidence_version: int = 1,
    tombstone_reason: str = "superseded",
    index_scope: str | None = None,
) -> dict[str, Any]:
    repo_id = _ensure_repo(conn, repo)
    files = files or []
    source_updated_at = 0.0 if source_updated_at is None else float(source_updated_at)
    previous = conn.execute(
        "SELECT latest_manifest_id FROM prs WHERE repo_id=? AND number=?",
        (repo_id, pr_number),
    ).fetchone()
    parent_id = int(previous[0]) if previous and previous[0] is not None else None
    canonical_files: list[dict[str, Any]] = []
    for f in files:
        path = str(f.get("path", ""))
        raw_patch = str(f.get("patch_snippet", f.get("patch", "")))
        patch = _truncate_bytes(raw_patch, int(f.get("snippet_max_bytes", DEFAULT_PATCH_SNIPPET_BYTES)))
        patch_hash = hashlib.sha256(raw_patch.encode()).hexdigest()
        diff_terms = " ".join(sorted(_terms([path, patch, f.get("diff_terms", "")])))
        hunk_payload = {
            "path": path,
            "patch_hash": patch_hash,
            "diff_terms": diff_terms,
            "status": f.get("status", "modified"),
            "additions": int(f.get("additions", 0)),
            "deletions": int(f.get("deletions", 0)),
        }
        hunk_hash = hashlib.sha256(_json(hunk_payload).encode()).hexdigest()
        canonical_files.append({
            **hunk_payload,
            "patch_snippet": patch,
            "hunk_hash": hunk_hash,
        })
    payload = {
        "repo": repo,
        "pr": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "title": title,
        "body": body,
        "files": canonical_files,
        "evidence_version": evidence_version,
    }
    mh = _manifest_hash(payload)
    row = conn.execute(
        "SELECT id FROM pr_revision_manifests WHERE manifest_hash=?", (mh,)
    ).fetchone()
    if row:
        manifest_id = int(row[0])
    else:
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        cur = conn.execute(
            """INSERT INTO pr_revision_manifests(repo_id,pr_number,head_sha,base_sha,captured_at,source_updated_at,evidence_version,body_hash,manifest_hash,parent_manifest_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                repo_id,
                pr_number,
                head_sha,
                base_sha,
                _now(),
                source_updated_at,
                evidence_version,
                body_hash,
                mh,
                parent_id,
            ),
        )
        manifest_id = int(cur.lastrowid)
        for f in canonical_files:
            conn.execute(
                "INSERT INTO pr_revision_hunks(manifest_id,path,patch_hash,hunk_hash,patch_snippet,diff_terms,status,additions,deletions) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    manifest_id,
                    f["path"],
                    f["patch_hash"],
                    f["hunk_hash"],
                    f["patch_snippet"],
                    f["diff_terms"],
                    f["status"],
                    f["additions"],
                    f["deletions"],
                ),
            )
        manifest_terms = _terms([
            title,
            body,
            head_sha,
            base_sha,
            [f["path"] for f in canonical_files],
            [f["patch_snippet"] for f in canonical_files],
            [f["diff_terms"] for f in canonical_files],
        ])
        conn.executemany(
            "INSERT OR IGNORE INTO pr_revision_terms(manifest_id,term) VALUES(?,?)",
            [(manifest_id, term) for term in manifest_terms],
        )
    if parent_id and parent_id != manifest_id:
        conn.execute(
            "UPDATE pr_revision_manifests SET superseded_by_manifest_id=?, tombstoned_at=COALESCE(tombstoned_at,?), tombstone_reason=COALESCE(tombstone_reason,?) WHERE id=?",
            (manifest_id, _now(), tombstone_reason, parent_id),
        )
    conn.execute(
        """INSERT INTO prs(repo_id,number,state,title,body,url,head_sha,base_sha,source_updated_at,latest_manifest_id,evidence_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(repo_id,number) DO UPDATE SET state=excluded.state,title=excluded.title,body=excluded.body,url=excluded.url,head_sha=excluded.head_sha,base_sha=excluded.base_sha,source_updated_at=excluded.source_updated_at,latest_manifest_id=excluded.latest_manifest_id,evidence_version=excluded.evidence_version""",
        (
            repo_id,
            pr_number,
            state,
            title,
            body,
            url,
            head_sha,
            base_sha,
            source_updated_at,
            manifest_id,
            evidence_version,
        ),
    )
    if index_scope:
        scope = _scope_profile(index_scope)
        now = _now()
        conn.execute(
            """INSERT INTO pr_scope_membership(repo_id,pr_number,scope,first_indexed_at,last_indexed_at,source_updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(repo_id,pr_number,scope) DO UPDATE SET
                 last_indexed_at=excluded.last_indexed_at,
                 source_updated_at=excluded.source_updated_at""",
            (repo_id, pr_number, scope, now, now, source_updated_at),
        )
    return {
        "repo": repo,
        "pr_number": pr_number,
        "manifest_id": manifest_id,
        "manifest_hash": mh,
        "superseded_manifest_id": parent_id if parent_id != manifest_id else None,
    }


def record_indexer_run(
    db: str | Path | sqlite3.Connection,
    repo: str,
    scope: str,
    *,
    profile: str | None = None,
    started_at: float,
    finished_at: float,
    indexed: int,
    requests_used: int,
    capped: bool,
    stop_reason: str,
    max_requests: int | None = None,
    max_runtime_seconds: int | None = None,
    max_rss_mb: int | None = None,
    min_free_disk_bytes: int | None = None,
    batch_limit: int | None = None,
    max_files_per_pr: int | None = None,
    max_patch_bytes: int | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn, close = _connect(db)
    try:
        init_db(conn)
        sizes = _db_size_snapshot(db)
        normalized = _scope_profile(scope)
        cur = conn.execute(
            """INSERT INTO indexer_runs(repo,scope,profile,started_at,finished_at,indexed,requests_used,capped,stop_reason,
               max_requests,max_runtime_seconds,max_rss_mb,min_free_disk_bytes,batch_limit,max_files_per_pr,max_patch_bytes,
               db_bytes,wal_bytes,free_disk_bytes,detail_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                repo,
                normalized,
                profile or normalized,
                started_at,
                finished_at,
                indexed,
                requests_used,
                int(capped),
                stop_reason,
                max_requests,
                max_runtime_seconds,
                max_rss_mb,
                min_free_disk_bytes,
                batch_limit,
                max_files_per_pr,
                max_patch_bytes,
                sizes.get("db_bytes") or 0,
                sizes.get("wal_bytes") or 0,
                sizes.get("free_disk_bytes") or 0,
                _json(detail or {}),
            ),
        )
        conn.commit()
        return {
            "id": int(cur.lastrowid),
            "repo": repo,
            "scope": normalized,
            "profile": profile or normalized,
            "started_at": started_at,
            "finished_at": finished_at,
            "indexed": indexed,
            "requests_used": requests_used,
            "capped": capped,
            "stop_reason": stop_reason,
            **sizes,
        }
    finally:
        if close:
            conn.close()


def upsert_pr_revision(
    db: str | Path | sqlite3.Connection,
    repo: str,
    pr_number: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Append a revision manifest and make it the latest live PR revision."""
    conn, close = _connect(db)
    try:
        init_db(conn)
        result = _upsert_pr_revision_conn(conn, repo, pr_number, **kwargs)
        conn.commit()
        return result
    finally:
        if close:
            conn.close()


def refresh_pr(
    db: str | Path | sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    budget_available: bool = True,
    ttl_seconds: int = 300,
    failure_reason: str | None = None,
    lease_ttl_seconds: int | None = None,
    **revision: Any,
) -> dict[str, Any]:
    """Mark one PR refreshed and issue a short-lived revision lease.

    Passing revision fields also upserts a new latest revision; omitting them
    refreshes the existing local evidence only.
    """
    if lease_ttl_seconds is not None:
        ttl_seconds = lease_ttl_seconds
    if not budget_available:
        return {
            "repo": repo,
            "pr_number": pr_number,
            "refresh_status": "suggestion_only",
            "lease_id": None,
        }
    if failure_reason:
        failure = record_refresh_failure(db, repo, pr_number, failure_reason)
        return {
            "repo": repo,
            "pr_number": pr_number,
            "refresh_status": "dead_lettered",
            "lease_id": None,
            "dead_letter": failure,
        }
    conn, close = _connect(db)
    try:
        init_db(conn)
        if revision:
            _upsert_pr_revision_conn(conn, repo, pr_number, **revision)
        repo_id = _ensure_repo(conn, repo)
        row = conn.execute(
            "SELECT head_sha,base_sha,evidence_version FROM prs WHERE repo_id=? AND number=?",
            (repo_id, pr_number),
        ).fetchone()
        if not row:
            raise KeyError(f"PR not indexed: {repo}#{pr_number}")
        issued = _now()
        lease_id = uuid.uuid4().hex
        conn.execute(
            "UPDATE prs SET last_refreshed_at=? WHERE repo_id=? AND number=?",
            (issued, repo_id, pr_number),
        )
        conn.execute(
            "INSERT INTO refresh_leases VALUES(?,?,?,?,?,?,?,?,?)",
            (
                lease_id,
                repo_id,
                pr_number,
                row[0],
                row[1],
                int(row[2]),
                issued,
                issued + ttl_seconds,
                "fresh",
            ),
        )
        conn.commit()
        return {
            "repo": repo,
            "pr_number": pr_number,
            "lease_id": lease_id,
            "refresh_status": "fresh",
            "head_sha": row[0],
            "base_sha": row[1],
            "evidence_version": int(row[2]),
            "expires_at": issued + ttl_seconds,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        if close:
            conn.close()


def _serving_scope_sql(alias: str = "p") -> str:
    return f"""(
             NOT EXISTS (
               SELECT 1 FROM pr_scope_membership sm
               WHERE sm.repo_id={alias}.repo_id AND sm.pr_number={alias}.number
             )
             OR EXISTS (
               SELECT 1 FROM pr_scope_membership sm
               WHERE sm.repo_id={alias}.repo_id AND sm.pr_number={alias}.number
                 AND sm.scope!='cold_archive'
             )
           )"""


def _candidate_rows(conn: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    owner, name = _repo_parts(repo)
    return list(
        conn.execute(
            """SELECT p.*, r.owner||'/'||r.name AS repo, m.manifest_hash
	           FROM prs p JOIN repos r ON r.id=p.repo_id JOIN pr_revision_manifests m ON m.id=p.latest_manifest_id
	           WHERE r.owner=? AND r.name=? AND """
            + _serving_scope_sql("p"),
            (owner, name),
        )
    )


def _latest_manifest_term_coverage(
    conn: sqlite3.Connection, repo: str
) -> tuple[int, int]:
    owner, name = _repo_parts(repo)
    row = conn.execute(
        """SELECT COUNT(DISTINCT p.latest_manifest_id) AS latest_count,
                  COUNT(DISTINCT t.manifest_id) AS term_indexed_count
           FROM prs p
           JOIN repos r ON r.id=p.repo_id
           LEFT JOIN pr_revision_terms t ON t.manifest_id=p.latest_manifest_id
	           WHERE r.owner=? AND r.name=? AND p.latest_manifest_id IS NOT NULL
	             AND """
            + _serving_scope_sql("p"),
        (owner, name),
    ).fetchone()
    if not row:
        return 0, 0
    return int(row["latest_count"] or 0), int(row["term_indexed_count"] or 0)


def _candidate_rows_for_terms(
    conn: sqlite3.Connection, repo: str, terms: set[str]
) -> tuple[list[sqlite3.Row], dict[str, Any]]:
    """Return latest-live PR rows narrowed by indexed terms when safe.

    Term narrowing is an optimization only. If a migrated/mixed DB has any
    latest manifests without term rows, fall back to all latest-live candidates
    so overlap search keeps recall over speed.
    """
    meta = {
        "enabled": False,
        "terms_considered": len(terms),
        "reason": "no_terms",
        "latest_manifest_count": 0,
        "term_indexed_manifest_count": 0,
    }
    if not terms:
        return _candidate_rows(conn, repo), meta
    latest_count, term_indexed_count = _latest_manifest_term_coverage(conn, repo)
    meta["latest_manifest_count"] = latest_count
    meta["term_indexed_manifest_count"] = term_indexed_count
    if len(terms) > 80:
        meta["reason"] = "too_many_terms"
        return _candidate_rows(conn, repo), meta
    if latest_count != term_indexed_count:
        meta["reason"] = "incomplete_term_index"
        return _candidate_rows(conn, repo), meta
    owner, name = _repo_parts(repo)
    selected = sorted(terms)[:80]
    placeholders = ",".join("?" for _ in selected)
    rows = list(
        conn.execute(
            f"""SELECT DISTINCT p.*, r.owner||'/'||r.name AS repo, m.manifest_hash
               FROM pr_revision_terms t
               JOIN pr_revision_manifests m ON m.id=t.manifest_id
               JOIN prs p ON p.repo_id=m.repo_id AND p.number=m.pr_number
                    AND p.latest_manifest_id=m.id
               JOIN repos r ON r.id=p.repo_id
	               WHERE r.owner=? AND r.name=? AND t.term IN ({placeholders})
	                 AND """
                + _serving_scope_sql("p"),
	            (owner, name, *selected),
	        )
	    )
    if not rows:
        meta["reason"] = "no_term_matches"
        return _candidate_rows(conn, repo), meta
    meta["enabled"] = True
    meta["reason"] = "term_match"
    return rows, meta


def _manifest_files(conn: sqlite3.Connection, manifest_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT path,patch_hash,hunk_hash,patch_snippet,diff_terms,status,additions,deletions FROM pr_revision_hunks WHERE manifest_id=?",
            (manifest_id,),
        )
    ]


def _lease(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM refresh_leases WHERE repo_id=? AND pr_number=? AND head_sha=? AND base_sha=? AND evidence_version=? AND expires_at>? ORDER BY issued_at DESC LIMIT 1""",
        (
            row["repo_id"],
            row["number"],
            row["head_sha"],
            row["base_sha"],
            row["evidence_version"],
            _now(),
        ),
    ).fetchone()


def _capsule(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    issue: dict[str, Any],
    matched: list[str],
    unmatched: list[str],
    classification: str,
    confidence: float,
    lease_id: str,
) -> str:
    files = _manifest_files(conn, int(row["latest_manifest_id"]))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "score_version": SCORE_VERSION,
        "matcher_version": MATCHER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "repo": row["repo"],
        "issue_number": issue.get("issue_number"),
        "pr_number": row["number"],
        "classification": classification,
        "confidence": confidence,
        "lease_id": lease_id,
        "revision": {
            "head_sha": row["head_sha"],
            "base_sha": row["base_sha"],
            "evidence_version": row["evidence_version"],
            "manifest_hash": row["manifest_hash"],
        },
        "issue_input": issue,
        "matched_anchors": matched,
        "unmatched_anchors": unmatched,
        "evidence": {"title": row["title"], "body": row["body"], "files": files},
    }
    capsule_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
    capsule_id = capsule_hash[:24]
    conn.execute(
        "INSERT OR REPLACE INTO replay_capsules VALUES(?,?,?,?,?,?,?,?)",
        (
            capsule_id,
            row["repo_id"],
            issue.get("issue_number"),
            row["number"],
            classification,
            capsule_hash,
            _json(payload),
            _now(),
        ),
    )
    return capsule_id


def search_pr_overlap(
    db: str | Path | sqlite3.Connection,
    repo: str,
    *,
    issue_number: int | None = None,
    title: str = "",
    body: str = "",
    symptoms: list[str] | None = None,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    error_messages: list[str] | None = None,
    labels: list[str] | None = None,
    top_k: int = 10,
    require_fresh: bool = False,
    refresh_budget_available: bool = True,
    refresh_failure_reason: str | None = None,
) -> dict[str, Any]:
    """Return ranked local overlap candidates.

    The matcher is intentionally transparent: title/body/symptom/error/file/
    symbol anchors are matched against latest PR metadata and latest-live file
    hunks. Superseded manifests can surface only as `historical_overlap_only`.
    """
    conn, close = _connect_existing(db, validate_schema=True)
    try:
        import_locked = _import_lockfile_exists(db)
        mutated = False
        issue = {
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "symptoms": symptoms or [],
            "files": files or [],
            "symbols": symbols or [],
            "error_messages": error_messages or [],
            "labels": labels or [],
        }
        strong = _terms([files or [], symbols or [], error_messages or []])
        all_terms = _terms([title, body, symptoms or [], labels or []]) | strong
        results: list[dict[str, Any]] = []

        def overlap_classification(
            matched_strong: list[str], unmatched: list[str]
        ) -> str:
            if (
                strong
                and not unmatched
                and len(matched_strong) >= max(1, min(2, len(strong)))
            ):
                return "full-cover"
            if matched_strong:
                return "partial-cover"
            return "adjacent"

        def freshness_class(row: sqlite3.Row) -> str:
            if (
                row["last_refreshed_at"]
                and row["last_refreshed_at"] >= row["source_updated_at"]
            ):
                return "fresh"
            return "needs_refresh"

        candidate_rows, candidate_prefilter = _candidate_rows_for_terms(
            conn, repo, strong or all_terms
        )
        for row in candidate_rows:
            frows = _manifest_files(conn, int(row["latest_manifest_id"]))
            haystack = _terms([
                row["title"],
                row["body"],
                [f["path"] for f in frows],
                [f["patch_snippet"] for f in frows],
                [f["diff_terms"] for f in frows],
            ])
            matched_terms = sorted(all_terms & haystack)
            matched_strong = sorted(strong & haystack)
            unmatched = sorted(strong - haystack)
            score = len(matched_terms) + (2 * len(matched_strong))
            if issue_number and re.search(
                rf"(?:#|issue\s+){issue_number}\b",
                f"{row['title']} {row['body']}",
                re.I,
            ):
                score += 5
                matched_terms.append(f"issue:{issue_number}")
            if score <= 0:
                continue
            classification = overlap_classification(matched_strong, unmatched)
            freshness = freshness_class(row)
            lease = _lease(conn, row)
            lease_status = "live" if lease else "missing"
            capsule_id = None
            archive_allowed = False
            refresh_status = "fresh" if lease else "missing"
            dead_letter = None
            if classification == "full-cover" and require_fresh:
                if not lease:
                    classification = "needs-refresh"
                    freshness = "needs_refresh"
                    if import_locked:
                        refresh_status = "import_lock_held"
                    elif refresh_failure_reason:
                        dead_letter = record_refresh_failure(
                            conn, repo, int(row["number"]), refresh_failure_reason
                        )
                        refresh_status = "dead_lettered"
                        mutated = True
                    elif not refresh_budget_available:
                        refresh_status = "suggestion_only"
                    else:
                        refresh_status = "missing"
                else:
                    if import_locked:
                        classification = "needs-refresh"
                        refresh_status = "import_lock_held"
                        freshness = "needs_refresh"
                    else:
                        refresh_status = "fresh"
                        capsule_id = _capsule(
                            conn,
                            row,
                            issue,
                            sorted(set(matched_terms)),
                            unmatched,
                            classification,
                            min(0.99, 0.55 + score / 20),
                            lease["lease_id"],
                        )
                        mutated = True
                        archive_allowed = True
                        freshness = "fresh"
            elif classification == "full-cover" and not require_fresh:
                if freshness != "fresh" or not lease:
                    classification = "needs-refresh"
                else:
                    classification = "full-cover"
            result = {
                "repo": repo,
                "pr_number": row["number"],
                "score": score,
                "classification": classification,
                "confidence": round(min(0.99, 0.45 + score / 20), 3),
                "freshness_class": freshness,
                "refresh_status": refresh_status,
                "head_sha": row["head_sha"],
                "base_sha": row["base_sha"],
                "evidence_version": row["evidence_version"],
                "source_updated_at": row["source_updated_at"],
                "last_refreshed_at": row["last_refreshed_at"],
                "matched_anchors": sorted(set(matched_terms)),
                "unmatched_anchors": unmatched,
                "lease_status": lease_status,
                "lease_id": lease["lease_id"] if lease else None,
                "capsule_id": capsule_id,
                "archive_allowed": archive_allowed,
            }
            if dead_letter is not None:
                result["dead_letter"] = dead_letter
            results.append(result)
        # Historical evidence is advisory only.
        owner, name = _repo_parts(repo)
        for h in conn.execute(
	            """SELECT r.owner||'/'||r.name AS repo,m.*,p.number AS current_number
	               FROM pr_revision_manifests m
	               JOIN repos r ON r.id=m.repo_id
	               JOIN prs p ON p.repo_id=m.repo_id AND p.number=m.pr_number
	               WHERE r.owner=? AND r.name=? AND m.superseded_by_manifest_id IS NOT NULL
	                 AND """
                + _serving_scope_sql("p"),
	            (owner, name),
	        ):
            frows = _manifest_files(conn, int(h["id"]))
            haystack = _terms([
                h["head_sha"],
                [f["path"] for f in frows],
                [f["patch_snippet"] for f in frows],
                [f["diff_terms"] for f in frows],
            ])
            matched = sorted(all_terms & haystack)
            if matched:
                results.append({
                    "repo": repo,
                    "pr_number": h["pr_number"],
                    "score": len(matched),
                    "classification": "historical_overlap_only",
                    "confidence": 0.4,
                    "freshness_class": "historical",
                    "head_sha": h["head_sha"],
                    "base_sha": h["base_sha"],
                    "evidence_version": h["evidence_version"],
                    "source_updated_at": h["source_updated_at"],
                    "last_refreshed_at": None,
                    "matched_anchors": matched,
                    "unmatched_anchors": sorted(strong - haystack),
                    "lease_status": "not_applicable",
                    "lease_id": None,
                    "capsule_id": None,
                    "archive_allowed": False,
                })
        results.sort(key=lambda r: (r["archive_allowed"], r["score"]), reverse=True)
        if mutated:
            conn.commit()
        return {
            "repo": repo,
            "query": issue,
            "results": results[:top_k],
            "result_count": min(len(results), top_k),
            "candidate_prefilter": candidate_prefilter,
        }
    finally:
        if close:
            conn.close()


def get_pr_evidence(
    db: str | Path | sqlite3.Connection, repo: str, pr_number: int
) -> dict[str, Any]:
    conn, close = _connect_existing(db, validate_schema=True)
    try:
        owner, name = _repo_parts(repo)
        row = conn.execute(
            """SELECT p.*, r.owner||'/'||r.name AS repo, m.manifest_hash FROM prs p JOIN repos r ON r.id=p.repo_id JOIN pr_revision_manifests m ON m.id=p.latest_manifest_id WHERE r.owner=? AND r.name=? AND p.number=?""",
            (owner, name, pr_number),
        ).fetchone()
        if not row:
            raise KeyError(f"PR not indexed: {repo}#{pr_number}")
        lease = _lease(conn, row)

        def freshness_class(row: sqlite3.Row) -> str:
            if (
                row["last_refreshed_at"]
                and row["last_refreshed_at"] >= row["source_updated_at"]
            ):
                return "fresh"
            return "needs_refresh"

        return {
            "repo": repo,
            "pr_number": pr_number,
            "title": row["title"],
            "body": row["body"],
            "state": row["state"],
            "revision": {
                "head_sha": row["head_sha"],
                "base_sha": row["base_sha"],
                "evidence_version": row["evidence_version"],
                "manifest_hash": row["manifest_hash"],
            },
            "last_refreshed_at": row["last_refreshed_at"],
            "source_updated_at": row["source_updated_at"],
            "freshness_class": freshness_class(row),
            "lease_status": "live" if lease else "missing",
            "lease_id": lease["lease_id"] if lease else None,
            "files": _manifest_files(conn, int(row["latest_manifest_id"])),
        }
    finally:
        if close:
            conn.close()


def record_refresh_failure(
    db: str | Path | sqlite3.Connection,
    repo: str,
    pr_number: int,
    reason_class: str,
    error_summary: str = "",
    *,
    retry_after_seconds: int = 60,
    terminal: bool = False,
) -> dict[str, Any]:
    conn, close = _connect(db)
    try:
        init_db(conn)
        repo_id = _ensure_repo(conn, repo)
        now = _now()
        existing = conn.execute(
            "SELECT attempt_count, first_failed_at FROM refresh_dead_letters WHERE repo_id=? AND pr_number=? AND reason_class=?",
            (repo_id, pr_number, reason_class),
        ).fetchone()
        if existing:
            attempts = int(existing[0]) + 1
            first = float(existing[1])
            conn.execute(
                "UPDATE refresh_dead_letters SET attempt_count=?, last_failed_at=?, next_retry_at=?, last_error_summary=?, terminal=? WHERE repo_id=? AND pr_number=? AND reason_class=?",
                (
                    attempts,
                    now,
                    now + retry_after_seconds,
                    error_summary,
                    int(terminal),
                    repo_id,
                    pr_number,
                    reason_class,
                ),
            )
        else:
            attempts = 1
            first = now
            conn.execute(
                "INSERT INTO refresh_dead_letters(repo_id,pr_number,reason_class,first_failed_at,last_failed_at,attempt_count,next_retry_at,last_error_summary,terminal) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    repo_id,
                    pr_number,
                    reason_class,
                    first,
                    now,
                    attempts,
                    now + retry_after_seconds,
                    error_summary,
                    int(terminal),
                ),
            )
        conn.commit()
        return {
            "repo": repo,
            "pr_number": pr_number,
            "reason_class": reason_class,
            "first_failed_at": first,
            "last_failed_at": now,
            "attempt_count": attempts,
            "next_retry_at": now + retry_after_seconds,
            "last_error_summary": error_summary,
            "terminal": bool(terminal),
        }
    finally:
        if close:
            conn.close()


def get_dead_letters(
    db: str | Path | sqlite3.Connection, repo: str | None = None
) -> list[dict[str, Any]]:
    conn, close = _connect_existing(db, validate_schema=True)
    try:
        params: tuple[Any, ...] = ()
        where = ""
        if repo:
            owner, name = _repo_parts(repo)
            where = "WHERE r.owner=? AND r.name=?"
            params = (owner, name)
        return [
            dict(r)
            for r in conn.execute(
                f"""SELECT d.*, r.owner||'/'||r.name AS repo FROM refresh_dead_letters d JOIN repos r ON r.id=d.repo_id {where} ORDER BY d.last_failed_at DESC""",
                params,
            )
        ]
    finally:
        if close:
            conn.close()


def _replay_payload(capsule: dict[str, Any]) -> dict[str, Any]:
    payload = (
        capsule.get("payload") if isinstance(capsule.get("payload"), dict) else capsule
    )
    expected_hash = (
        capsule.get("capsule_hash")
        or capsule.get("hash")
        or payload.get("capsule_hash")
    )
    actual_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
    ok = actual_hash == expected_hash if expected_hash else None
    return {
        "capsule_id": capsule.get("capsule_id"),
        "ok": ok,
        "classification": payload.get("classification"),
        "repo": payload.get("repo"),
        "pr_number": payload.get("pr_number"),
        "payload": payload,
    }


def replay_capsule(
    db: str | Path | sqlite3.Connection, capsule: str | dict[str, Any]
) -> dict[str, Any]:
    if isinstance(capsule, dict):
        return _replay_payload(capsule)
    try:
        decoded = json.loads(capsule)
    except (TypeError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, dict):
        return _replay_payload(decoded)
    capsule_id = capsule
    conn, close = _connect_existing(db, validate_schema=True)
    try:
        row = conn.execute(
            "SELECT * FROM replay_capsules WHERE capsule_id=?", (capsule_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Capsule not found: {capsule_id}")
        payload = _load(row["payload_json"], {})
        ok = hashlib.sha256(_json(payload).encode()).hexdigest() == row["capsule_hash"]
        return {
            "capsule_id": capsule_id,
            "ok": ok,
            "classification": payload.get("classification"),
            "repo": payload.get("repo"),
            "pr_number": payload.get("pr_number"),
            "payload": payload,
        }
    finally:
        if close:
            conn.close()


def gc_replay_capsules(
    db: str | Path | sqlite3.Connection,
    *,
    older_than: float | None = None,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Garbage-collect replay capsules without touching revision evidence."""
    conn, close = _connect(db)
    try:
        init_db(conn)
        delete_ids: set[str] = set()
        if older_than is not None:
            delete_ids.update(
                str(r[0])
                for r in conn.execute(
                    "SELECT capsule_id FROM replay_capsules WHERE emitted_at<?",
                    (older_than,),
                )
            )
        if max_rows is not None:
            rows = list(
                conn.execute(
                    "SELECT capsule_id FROM replay_capsules ORDER BY emitted_at DESC"
                )
            )
            for row in rows[max_rows:]:
                delete_ids.add(str(row[0]))
        if max_bytes is not None:
            total = int(
                _scalar(
                    conn,
                    "SELECT COALESCE(SUM(LENGTH(payload_json)),0) FROM replay_capsules",
                )
                or 0
            )
            if total > max_bytes:
                rows = list(
                    conn.execute(
                        "SELECT capsule_id,LENGTH(payload_json) AS bytes FROM replay_capsules ORDER BY emitted_at ASC"
                    )
                )
                for row in rows:
                    if total <= max_bytes:
                        break
                    capsule_id = str(row["capsule_id"])
                    if capsule_id not in delete_ids:
                        delete_ids.add(capsule_id)
                        total -= int(row["bytes"] or 0)
        for capsule_id in sorted(delete_ids):
            conn.execute(
                "DELETE FROM replay_capsules WHERE capsule_id=?", (capsule_id,)
            )
        conn.commit()
        return {
            "deleted": len(delete_ids),
            "remaining": int(_scalar(conn, "SELECT COUNT(*) FROM replay_capsules") or 0),
            "historical_anchor_count": int(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pr_revision_manifests WHERE superseded_by_manifest_id IS NOT NULL OR tombstoned_at IS NOT NULL",
                )
                or 0
            ),
        }
    finally:
        if close:
            conn.close()


def import_disk_preflight(
    live_db: str | Path,
    staging_db: str | Path,
    *,
    min_free_disk_bytes: int = 4 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    """Check whether an import can safely hold live, staging, rollback, and reserve.

    This is a conservative preflight used before copying or activating a cold
    import artifact on a small VPS.  It performs only local filesystem checks.
    """
    live = Path(live_db)
    staging = Path(staging_db)
    live_family_bytes = sum(_path_size(p) for p in _db_family_paths(live))
    staging_family_bytes = sum(_path_size(p) for p in _db_family_paths(staging))
    try:
        usage = os.statvfs(live.expanduser().resolve().parent)
        free_disk_bytes = int(usage.f_bavail * usage.f_frsize)
    except OSError:
        free_disk_bytes = 0
    required_bytes = (
        live_family_bytes
        + staging_family_bytes
        + live_family_bytes
        + min_free_disk_bytes
    )
    ok = free_disk_bytes >= required_bytes
    return {
        "ok": ok,
        "reason": "ok" if ok else "insufficient_disk",
        "live_family_bytes": live_family_bytes,
        "staging_family_bytes": staging_family_bytes,
        "rollback_family_bytes": live_family_bytes,
        "min_free_disk_bytes": min_free_disk_bytes,
        "required_free_disk_bytes": required_bytes,
        "free_disk_bytes": free_disk_bytes,
    }


def validate_staging_import_db(staging_db: str | Path) -> dict[str, Any]:
    """Validate a staging DB before import activation."""
    path = Path(staging_db)
    if not path.exists():
        return {"ok": False, "reason": "missing_staging_db"}
    conn = sqlite3.connect(str(path))
    try:
        _configure_connection(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return {"ok": False, "reason": "integrity_check_failed", "detail": integrity}
        try:
            _validate_schema(conn)
        except IndexNotInitializedError as exc:
            return {"ok": False, "reason": "schema_validation_failed", "detail": str(exc)}
        return {"ok": True, "reason": "ok", "schema_version": SCHEMA_VERSION}
    finally:
        conn.close()


def _checkpoint_db(path: Path) -> None:
    if not path.exists():
        return
    conn = sqlite3.connect(str(path))
    try:
        _configure_connection(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _unlink_db_sidecars(path: Path) -> None:
    for sidecar in _db_family_paths(path)[1:]:
        sidecar.unlink(missing_ok=True)


def _active_locks(
    conn: sqlite3.Connection, *, now: float | None = None, stale_after_seconds: int = 900
) -> list[sqlite3.Row]:
    now = _now() if now is None else now
    conn.execute(
        "DELETE FROM indexer_locks WHERE heartbeat_at<?",
        (now - stale_after_seconds,),
    )
    return list(conn.execute("SELECT * FROM indexer_locks ORDER BY name"))


def _acquire_import_lockfile(
    db: str | Path, *, holder: str, stale_after_seconds: int = 900
) -> tuple[bool, str]:
    lock_path = _import_lock_path(db)
    now = _now()
    _ = stale_after_seconds
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_json({"holder": holder, "acquired_at": now}))
    except FileExistsError:
        return False, f"active_lockfile:{lock_path}"
    return True, str(lock_path)


def _live_db_lock_conflict(
    db: str | Path, *, stale_after_seconds: int = 900
) -> str | None:
    path = Path(db)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    _configure_connection(conn)
    if not _table_exists(conn, "indexer_locks"):
        conn.close()
        return None
    now = _now()
    locks = _active_locks(conn, now=now, stale_after_seconds=stale_after_seconds)
    conn.commit()
    conn.close()
    if locks:
        names = ",".join(str(row["name"]) for row in locks)
        return f"active_lock:{names}"
    return None


def _release_import_lockfile(db: str | Path) -> None:
    _import_lock_path(db).unlink(missing_ok=True)


def _repo_names(conn: sqlite3.Connection) -> list[str]:
    return [
        f"{row['owner']}/{row['name']}"
        for row in conn.execute("SELECT owner,name FROM repos ORDER BY owner,name")
    ]


def _import_manifest_for_db(db: str | Path) -> dict[str, Any]:
    conn = open_existing_index_db(db, validate_schema=True)
    try:
        return {
            "schema_version": SCHEMA_VERSION,
            "index_version": INDEX_VERSION,
            "repos": _repo_names(conn),
            "row_counts": {
                "repos": int(_scalar(conn, "SELECT COUNT(*) FROM repos") or 0),
                "prs": int(_scalar(conn, "SELECT COUNT(*) FROM prs") or 0),
                "manifests": int(
                    _scalar(conn, "SELECT COUNT(*) FROM pr_revision_manifests") or 0
                ),
                "hunks": int(
                    _scalar(conn, "SELECT COUNT(*) FROM pr_revision_hunks") or 0
                ),
                "terms": int(
                    _scalar(conn, "SELECT COUNT(*) FROM pr_revision_terms") or 0
                ),
            },
            **_db_size_snapshot(db),
        }
    finally:
        conn.close()


def export_cold_import_bundle(
    source_db: str | Path,
    bundle_dir: str | Path,
    *,
    repo: str | None = None,
) -> dict[str, Any]:
    """Export a validated DB artifact for later cold import activation."""
    source = Path(source_db)
    bundle = Path(bundle_dir)
    if not source.exists():
        return {"ok": False, "reason": "missing_source_db"}
    bundle.mkdir(parents=True, exist_ok=True)
    artifact = bundle / "pr-overlap-import.db"
    manifest_path = bundle / "manifest.json"
    tmp_artifact = bundle / f".{artifact.name}.{uuid.uuid4().hex}.tmp"
    _checkpoint_db(source)
    source_conn = open_existing_index_db(source, validate_schema=True)
    tmp_conn = sqlite3.connect(str(tmp_artifact))
    try:
        source_conn.backup(tmp_conn)
    finally:
        tmp_conn.close()
        source_conn.close()
    validation = validate_staging_import_db(tmp_artifact)
    if not validation.get("ok"):
        _unlink_db_sidecars(tmp_artifact)
        tmp_artifact.unlink(missing_ok=True)
        return {"ok": False, "reason": "artifact_validation_failed", "validation": validation}
    manifest = _import_manifest_for_db(tmp_artifact)
    if repo and repo not in manifest["repos"]:
        _unlink_db_sidecars(tmp_artifact)
        tmp_artifact.unlink(missing_ok=True)
        return {"ok": False, "reason": "repo_not_in_source", "repo": repo}
    manifest.update({
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "artifact": artifact.name,
        "source_db": str(source),
        "created_at": _now(),
    })
    _unlink_db_sidecars(tmp_artifact)
    os.replace(tmp_artifact, artifact)
    tmp_manifest = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    tmp_manifest.write_text(_json(manifest), encoding="utf-8")
    os.replace(tmp_manifest, manifest_path)
    return {
        "ok": True,
        "reason": "ok",
        "bundle_dir": str(bundle),
        "artifact": str(artifact),
        "manifest": str(manifest_path),
        **manifest,
    }


def validate_cold_import_bundle(
    bundle_dir: str | Path,
    *,
    expected_repo: str | None = None,
) -> dict[str, Any]:
    """Validate a cold import bundle manifest and SQLite artifact."""
    bundle = Path(bundle_dir)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "reason": "missing_manifest"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "invalid_manifest", "detail": str(exc)}
    if manifest.get("bundle_schema_version") != BUNDLE_SCHEMA_VERSION:
        return {
            "ok": False,
            "reason": "unsupported_bundle_schema",
            "expected": BUNDLE_SCHEMA_VERSION,
            "actual": manifest.get("bundle_schema_version"),
        }
    raw_artifact = str(manifest.get("artifact") or "pr-overlap-import.db")
    artifact_name = Path(raw_artifact)
    if artifact_name.is_absolute():
        return {"ok": False, "reason": "artifact_outside_bundle", "artifact": raw_artifact}
    bundle_root = bundle.resolve()
    artifact = (bundle / artifact_name).resolve()
    try:
        artifact.relative_to(bundle_root)
    except ValueError:
        return {"ok": False, "reason": "artifact_outside_bundle", "artifact": raw_artifact}
    validation = validate_staging_import_db(artifact)
    if not validation.get("ok"):
        return {"ok": False, "reason": "artifact_validation_failed", "validation": validation}
    actual = _import_manifest_for_db(artifact)
    for key in ("schema_version", "index_version", "repos", "row_counts"):
        if manifest.get(key) != actual.get(key):
            return {
                "ok": False,
                "reason": "manifest_mismatch",
                "field": key,
                "expected": manifest.get(key),
                "actual": actual.get(key),
            }
    if expected_repo and expected_repo not in actual["repos"]:
        return {"ok": False, "reason": "repo_not_in_bundle", "repo": expected_repo}
    return {
        "ok": True,
        "reason": "ok",
        "bundle_dir": str(bundle),
        "artifact": str(artifact),
        "manifest": manifest,
        "actual": actual,
    }


def _copy_db_family(src_db: Path, dst_db: Path) -> list[str]:
    copied: list[str] = []
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    for src in _db_family_paths(src_db):
        suffix = src.name[len(src_db.name):]
        dst = dst_db.parent / f"{dst_db.name}{suffix}"
        if src.exists():
            shutil.copy2(src, dst)
            copied.append(str(dst))
        elif dst.exists():
            dst.unlink()
    return copied


def activate_cold_import_bundle(
    live_db: str | Path,
    bundle_dir: str | Path,
    *,
    expected_repo: str | None = None,
    rollback_dir: str | Path | None = None,
    min_free_disk_bytes: int = 4 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    """Atomically activate a validated cold import bundle with rollback backup."""
    live = Path(live_db)
    holder = uuid.uuid4().hex
    locked, lock_reason = _acquire_import_lockfile(live, holder=holder)
    if not locked:
        return {
            "ok": False,
            "reason": "import_lock_held",
            "detail": lock_reason,
        }
    try:
        bundle_validation = validate_cold_import_bundle(
            bundle_dir, expected_repo=expected_repo
        )
        if not bundle_validation.get("ok"):
            return {
                "ok": False,
                "reason": "bundle_validation_failed",
                "validation": bundle_validation,
            }
        lock_conflict = _live_db_lock_conflict(live)
        if lock_conflict:
            return {
                "ok": False,
                "reason": "import_lock_held",
                "detail": lock_conflict,
            }
        staging = Path(str(bundle_validation["artifact"]))
        preflight = import_disk_preflight(
            live, staging, min_free_disk_bytes=min_free_disk_bytes
        )
        if not preflight.get("ok"):
            return {"ok": False, "reason": "preflight_failed", "preflight": preflight}
        rollback_root = Path(rollback_dir) if rollback_dir else live.parent / "rollback"
        rollback_root.mkdir(parents=True, exist_ok=True)
        rollback_db = rollback_root / f"{live.name}.{int(_now())}.{uuid.uuid4().hex}.bak"
        if live.exists():
            _checkpoint_db(live)
        copied = _copy_db_family(live, rollback_db) if live.exists() else []
        live.parent.mkdir(parents=True, exist_ok=True)
        incoming = live.parent / f".{live.name}.{uuid.uuid4().hex}.incoming"
        shutil.copy2(staging, incoming)
        validation = validate_staging_import_db(incoming)
        if not validation.get("ok"):
            _unlink_db_sidecars(incoming)
            incoming.unlink(missing_ok=True)
            return {
                "ok": False,
                "reason": "incoming_validation_failed",
                "validation": validation,
                "rollback_db": str(rollback_db) if copied else None,
            }
        _unlink_db_sidecars(incoming)
        os.replace(incoming, live)
        _unlink_db_sidecars(live)
        return {
            "ok": True,
            "reason": "ok",
            "live_db": str(live),
            "rollback_db": str(rollback_db) if copied else None,
            "rollback_files": copied,
            "activated_artifact": str(staging),
            "preflight": preflight,
            "validation": validation,
        }
    finally:
        _release_import_lockfile(live)


def restore_import_backup(live_db: str | Path, rollback_db: str | Path) -> dict[str, Any]:
    """Restore a backup created by activate_cold_import_bundle."""
    live = Path(live_db)
    rollback = Path(rollback_db)
    if not rollback.exists():
        return {"ok": False, "reason": "missing_rollback_db"}
    holder = uuid.uuid4().hex
    locked, lock_reason = _acquire_import_lockfile(live, holder=holder)
    if not locked:
        return {"ok": False, "reason": "import_lock_held", "detail": lock_reason}
    try:
        lock_conflict = _live_db_lock_conflict(live)
        if lock_conflict:
            return {"ok": False, "reason": "import_lock_held", "detail": lock_conflict}
        validation = validate_staging_import_db(rollback)
        if not validation.get("ok"):
            return {"ok": False, "reason": "rollback_validation_failed", "validation": validation}
        if live.exists():
            _checkpoint_db(live)
        live.parent.mkdir(parents=True, exist_ok=True)
        incoming = live.parent / f".{live.name}.{uuid.uuid4().hex}.rollback"
        shutil.copy2(rollback, incoming)
        validation = validate_staging_import_db(incoming)
        if not validation.get("ok"):
            _unlink_db_sidecars(incoming)
            incoming.unlink(missing_ok=True)
            return {
                "ok": False,
                "reason": "incoming_rollback_validation_failed",
                "validation": validation,
            }
        _unlink_db_sidecars(incoming)
        os.replace(incoming, live)
        _unlink_db_sidecars(live)
        return {
            "ok": True,
            "reason": "ok",
            "live_db": str(live),
            "rollback_db": str(rollback),
            "validation": validation,
        }
    finally:
        _release_import_lockfile(live)


class PrOverlapIndex:
    """Small object wrapper around the module-level PR overlap API."""

    def __init__(self, db_path: str | Path | sqlite3.Connection):
        self.db_path = db_path

    def init_db(self) -> None:
        return init_db(self.db_path)

    def upsert_pr_revision(
        self, repo: str, number: int, **kwargs: Any
    ) -> dict[str, Any]:
        return upsert_pr_revision(self.db_path, repo, number, **kwargs)

    def refresh_pr(self, repo: str, number: int, **kwargs: Any) -> dict[str, Any]:
        return refresh_pr(self.db_path, repo, number, **kwargs)

    def search_pr_overlap(
        self,
        repo: str,
        issue: dict[str, Any] | None = None,
        require_fresh: bool = False,
        top_k: int = 5,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params = dict(issue or {})
        params.update(kwargs)
        return search_pr_overlap(
            self.db_path, repo, require_fresh=require_fresh, top_k=top_k, **params
        )

    def get_pr_evidence(self, repo: str, number: int) -> dict[str, Any]:
        return get_pr_evidence(self.db_path, repo, number)

    def record_refresh_failure(
        self,
        repo: str,
        number: int,
        reason_class: str,
        error_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        return record_refresh_failure(
            self.db_path, repo, number, reason_class, error_summary, **kwargs
        )

    def get_dead_letters(self, repo: str | None = None) -> list[dict[str, Any]]:
        return get_dead_letters(self.db_path, repo)

    def replay_capsule(self, capsule: str | dict[str, Any]) -> dict[str, Any]:
        return replay_capsule(self.db_path, capsule)

    def gc_replay_capsules(self, **kwargs: Any) -> dict[str, Any]:
        return gc_replay_capsules(self.db_path, **kwargs)
