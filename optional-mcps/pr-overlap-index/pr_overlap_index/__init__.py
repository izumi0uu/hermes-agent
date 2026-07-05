"""Compact SQLite-backed PR overlap index."""

from .core import (
    PrOverlapIndex,
    IndexNotInitializedError,
    get_dead_letters,
    get_pr_evidence,
    gc_replay_capsules,
    health_snapshot,
    init_db,
    open_existing_index_db,
    record_refresh_failure,
    refresh_pr,
    replay_capsule,
    search_pr_overlap,
    upsert_pr_revision,
)

__all__ = [
    "PrOverlapIndex",
    "IndexNotInitializedError",
    "get_dead_letters",
    "get_pr_evidence",
    "gc_replay_capsules",
    "health_snapshot",
    "init_db",
    "open_existing_index_db",
    "record_refresh_failure",
    "refresh_pr",
    "replay_capsule",
    "search_pr_overlap",
    "upsert_pr_revision",
]
