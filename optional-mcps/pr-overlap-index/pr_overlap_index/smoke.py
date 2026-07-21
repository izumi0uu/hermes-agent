"""Read-only smoke metrics for the PR overlap index."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from .core import IndexNotInitializedError, health_snapshot, search_pr_overlap

DEFAULT_REPO = "NousResearch/hermes-agent"
DEFAULT_TITLE = "context compressor tool args count replace write_file"
DEFAULT_BODY = "non string tool args crash"
DEFAULT_FILES = ["agent/context_compressor.py"]
DEFAULT_SYMBOLS = ["_summarize_tool_result"]


def _default_db() -> Path:
    explicit = os.environ.get("PR_OVERLAP_DB")
    if explicit:
        return Path(explicit)
    root = Path(os.environ.get("PR_OVERLAP_INDEX_ROOT", Path(__file__).resolve().parents[1]))
    return root / "pr-overlap.db"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def _db_term_coverage(db: Path) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    try:
        latest = conn.execute(
            "SELECT COUNT(*) FROM prs WHERE latest_manifest_id IS NOT NULL"
        ).fetchone()[0]
        indexed = conn.execute(
            "SELECT COUNT(DISTINCT manifest_id) FROM pr_revision_terms"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM pr_revision_terms").fetchone()[0]
        ratio = (float(indexed) / float(latest)) if latest else None
        return {
            "ok": True,
            "latest_manifest_count": int(latest),
            "term_indexed_manifest_count": int(indexed),
            "term_rows": int(rows),
            "term_index_coverage_ratio": ratio,
        }
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def _system_snapshot() -> dict[str, Any]:
    mem: dict[str, int] = {}
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                    mem[f"{key.lower()}_bytes"] = int(parts[1]) * 1024
    try:
        load1, load5, load15 = os.getloadavg()
        load = {"load1": load1, "load5": load5, "load15": load15}
    except OSError:
        load = {"load1": None, "load5": None, "load15": None}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname == "Darwin":
        rss_bytes = int(rss)
    else:
        rss_bytes = int(rss) * 1024
    return {**load, **mem, "smoke_max_rss_bytes": rss_bytes}


def _compact_results(results: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    compact = []
    for item in results[:limit]:
        compact.append({
            "pr_number": item.get("pr_number"),
            "score": item.get("score"),
            "classification": item.get("classification"),
            "archive_allowed": item.get("archive_allowed"),
            "refresh_status": item.get("refresh_status"),
            "matched_anchors": list(item.get("matched_anchors") or [])[:10],
        })
    return compact


def collect_smoke_metrics(
    *,
    db: str | Path,
    repo: str = DEFAULT_REPO,
    issue_number: int | None = 59291,
    title: str = DEFAULT_TITLE,
    body: str = DEFAULT_BODY,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    error_messages: list[str] | None = None,
    labels: list[str] | None = None,
    top_k: int = 5,
    runs: int = 5,
) -> dict[str, Any]:
    """Collect read-only health and query timing metrics."""
    db_path = Path(db)
    started = time.time()
    health = health_snapshot(db_path)
    term_coverage = _db_term_coverage(db_path) if health.get("ok") else {"ok": False}
    elapsed: list[float] = []
    query: dict[str, Any] = {
        "ran": False,
        "elapsed_ms_runs": [],
        "result_count": 0,
        "candidate_prefilter": None,
        "top_results": [],
    }
    if health.get("ok"):
        last_result: dict[str, Any] | None = None
        for _ in range(max(1, runs)):
            start = time.perf_counter()
            last_result = search_pr_overlap(
                db_path,
                repo,
                issue_number=issue_number,
                title=title,
                body=body,
                files=files if files is not None else list(DEFAULT_FILES),
                symbols=symbols if symbols is not None else list(DEFAULT_SYMBOLS),
                error_messages=error_messages or [],
                labels=labels or [],
                top_k=top_k,
            )
            elapsed.append(round((time.perf_counter() - start) * 1000, 2))
        results = list((last_result or {}).get("results") or [])
        query = {
            "ran": True,
            "elapsed_ms_runs": elapsed,
            "elapsed_ms_min": min(elapsed) if elapsed else None,
            "elapsed_ms_max": max(elapsed) if elapsed else None,
            "elapsed_ms_p50": statistics.median(elapsed) if elapsed else None,
            "elapsed_ms_p95": _percentile(elapsed, 0.95),
            "result_count": len(results),
            "candidate_prefilter": (last_result or {}).get("candidate_prefilter"),
            "top_results": _compact_results(results),
        }
    return {
        "ok": bool(health.get("ok")),
        "generated_at": started,
        "db_path": str(db_path),
        "repo": repo,
        "health": health,
        "term_coverage": term_coverage,
        "query": query,
        "system": _system_snapshot(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only PR overlap smoke metrics")
    parser.add_argument("--db", default=str(_default_db()))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--issue-number", type=int, default=59291)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--body", default=DEFAULT_BODY)
    parser.add_argument("--file", dest="files", action="append", default=None)
    parser.add_argument("--symbol", dest="symbols", action="append", default=None)
    parser.add_argument("--error", dest="error_messages", action="append", default=None)
    parser.add_argument("--label", dest="labels", action="append", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--fail-on-unhealthy", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = collect_smoke_metrics(
            db=args.db,
            repo=args.repo,
            issue_number=args.issue_number,
            title=args.title,
            body=args.body,
            files=args.files,
            symbols=args.symbols,
            error_messages=args.error_messages,
            labels=args.labels,
            top_k=args.top_k,
            runs=args.runs,
        )
    except IndexNotInitializedError as exc:
        payload = {"ok": False, "error": str(exc), "db_path": str(args.db)}
    print(json.dumps(payload, sort_keys=True))
    if args.fail_on_unhealthy and not payload.get("ok"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
