"""Small stdlib-only PR overlap index.

The module intentionally keeps V1 compact: SQLite stores immutable revision
manifests plus a mutable latest pointer; searches are local-only and archive
class (`full-cover`) answers require a live matching refresh lease and a replay
capsule.  No network or GitHub client code lives here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
SCORE_VERSION = "lexical-v1"
MATCHER_VERSION = "compact-v1"
NORMALIZATION_VERSION = "text-v1"
INDEX_VERSION = {
    "score": SCORE_VERSION,
    "matcher": MATCHER_VERSION,
    "normalization": NORMALIZATION_VERSION,
}


def _connect(db: str | Path | sqlite3.Connection) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db, sqlite3.Connection):
        db.row_factory = sqlite3.Row
        return db, False
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn, True


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
            """
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
    conn, close = _connect(db)
    try:
        init_db(conn)
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
        timestamps = [
            ts
            for ts in (latest_refresh, latest_source_update, latest_lease, latest_dlq)
            if ts
        ]
        return {
            "ok": True,
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
            "last_refreshed_at": latest_refresh,
            "last_source_updated_at": latest_source_update,
            "last_sync_at": max(timestamps) if timestamps else None,
        }
    finally:
        if close:
            conn.close()


def upsert_pr_revision(
    db: str | Path | sqlite3.Connection,
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
) -> dict[str, Any]:
    """Append a revision manifest and make it the latest live PR revision."""
    conn, close = _connect(db)
    try:
        init_db(conn)
        repo_id = _ensure_repo(conn, repo)
        files = files or []
        source_updated_at = source_updated_at or _now()
        previous = conn.execute(
            "SELECT latest_manifest_id FROM prs WHERE repo_id=? AND number=?",
            (repo_id, pr_number),
        ).fetchone()
        parent_id = int(previous[0]) if previous and previous[0] is not None else None
        payload = {
            "repo": repo,
            "pr": pr_number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "title": title,
            "body": body,
            "files": files,
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
            for f in files:
                path = str(f.get("path", ""))
                patch = str(f.get("patch_snippet", f.get("patch", "")))
                diff_terms = " ".join(
                    sorted(_terms([path, patch, f.get("diff_terms", "")]))
                )
                conn.execute(
                    "INSERT INTO pr_revision_hunks(manifest_id,path,patch_snippet,diff_terms,status,additions,deletions) VALUES(?,?,?,?,?,?,?)",
                    (
                        manifest_id,
                        path,
                        patch,
                        diff_terms,
                        f.get("status", "modified"),
                        int(f.get("additions", 0)),
                        int(f.get("deletions", 0)),
                    ),
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
        conn.commit()
        return {
            "repo": repo,
            "pr_number": pr_number,
            "manifest_id": manifest_id,
            "manifest_hash": mh,
            "superseded_manifest_id": parent_id if parent_id != manifest_id else None,
        }
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
            upsert_pr_revision(conn, repo, pr_number, **revision)
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
    finally:
        if close:
            conn.close()


def _candidate_rows(conn: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    owner, name = _repo_parts(repo)
    return list(
        conn.execute(
            """SELECT p.*, r.owner||'/'||r.name AS repo, m.manifest_hash
           FROM prs p JOIN repos r ON r.id=p.repo_id JOIN pr_revision_manifests m ON m.id=p.latest_manifest_id
           WHERE r.owner=? AND r.name=?""",
            (owner, name),
        )
    )


def _manifest_files(conn: sqlite3.Connection, manifest_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT path,patch_snippet,diff_terms,status,additions,deletions FROM pr_revision_hunks WHERE manifest_id=?",
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
    conn, close = _connect(db)
    try:
        init_db(conn)
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

        for row in _candidate_rows(conn, repo):
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
                    if refresh_failure_reason:
                        dead_letter = record_refresh_failure(
                            conn, repo, int(row["number"]), refresh_failure_reason
                        )
                        refresh_status = "dead_lettered"
                    elif not refresh_budget_available:
                        refresh_status = "suggestion_only"
                    else:
                        refresh_status = "missing"
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
            """SELECT r.owner||'/'||r.name AS repo,m.*,p.number AS current_number FROM pr_revision_manifests m JOIN repos r ON r.id=m.repo_id JOIN prs p ON p.repo_id=m.repo_id AND p.number=m.pr_number WHERE r.owner=? AND r.name=? AND m.superseded_by_manifest_id IS NOT NULL""",
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
        conn.commit()
        return {
            "repo": repo,
            "query": issue,
            "results": results[:top_k],
            "result_count": min(len(results), top_k),
        }
    finally:
        if close:
            conn.close()


def get_pr_evidence(
    db: str | Path | sqlite3.Connection, repo: str, pr_number: int
) -> dict[str, Any]:
    conn, close = _connect(db)
    try:
        init_db(conn)
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
    conn, close = _connect(db)
    try:
        init_db(conn)
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
    conn, close = _connect(db)
    try:
        init_db(conn)
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
