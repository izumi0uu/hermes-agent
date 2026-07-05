"""Compact SQLite-backed PR overlap index."""

from .core import (
    PrOverlapIndex,
    get_dead_letters,
    get_pr_evidence,
    health_snapshot,
    init_db,
    record_refresh_failure,
    refresh_pr,
    replay_capsule,
    search_pr_overlap,
    upsert_pr_revision,
)

__all__ = [
    "PrOverlapIndex",
    "get_dead_letters",
    "get_pr_evidence",
    "health_snapshot",
    "init_db",
    "record_refresh_failure",
    "refresh_pr",
    "replay_capsule",
    "search_pr_overlap",
    "upsert_pr_revision",
]
