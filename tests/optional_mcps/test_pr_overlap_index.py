import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = ROOT / "optional-mcps" / "pr-overlap-index"
sys.path.insert(0, str(PKG))

from pr_overlap_index import (  # noqa: E402
    IndexNotInitializedError,
    PrOverlapIndex,
    activate_cold_import_bundle,
    export_cold_import_bundle,
    gc_replay_capsules,
    get_dead_letters,
    get_pr_evidence,
    health_snapshot,
    import_disk_preflight,
    init_db,
    record_refresh_failure,
    refresh_pr,
    replay_capsule,
    restore_import_backup,
    search_pr_overlap,
    upsert_pr_revision,
    validate_cold_import_bundle,
    validate_staging_import_db,
)
from pr_overlap_index.indexer import (  # noqa: E402
    DEFAULT_MAX_RSS_MB,
    DEFAULT_MAX_RUNTIME_SECONDS,
    GitHubRestClient,
    GitHubNotFoundError,
    TransientGitHubError,
    RequestBudgetExceeded,
    normalize_scope,
    parse_github_timestamp,
    run_one_shot_indexer,
)
from pr_overlap_index.offline_backfill import (  # noqa: E402
    main as offline_backfill_main,
    run_backfill_loop,
)
from pr_overlap_index.smoke import collect_smoke_metrics  # noqa: E402


REPO = "NousResearch/hermes-agent"


def test_supersede_latest(tmp_path):
    db = tmp_path / "idx.db"
    init_db(db)
    first = upsert_pr_revision(
        db,
        REPO,
        10,
        title="old",
        files=[{"path": "cli.py", "patch_snippet": "fix old_error"}],
        head_sha="h1",
        base_sha="b1",
    )
    second = upsert_pr_revision(
        db,
        REPO,
        10,
        title="new",
        files=[{"path": "run_agent.py", "patch_snippet": "fix new_error"}],
        head_sha="h2",
        base_sha="b1",
    )
    assert first["manifest_id"] != second["manifest_id"]
    ev = get_pr_evidence(db, REPO, 10)
    assert ev["revision"]["head_sha"] == "h2"
    assert ev["files"][0]["path"] == "run_agent.py"


def test_full_cover_lease_and_capsule(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        11,
        title="Fix gateway timeout",
        files=[
            {
                "path": "gateway/run.py",
                "patch_snippet": "handle TimeoutError gateway_timeout",
            }
        ],
        head_sha="h1",
        base_sha="b1",
    )
    lease = refresh_pr(db, REPO, 11)
    out = search_pr_overlap(
        db,
        REPO,
        title="TimeoutError in gateway",
        files=["gateway/run.py"],
        error_messages=["TimeoutError"],
        require_fresh=True,
    )
    hit = out["results"][0]
    assert hit["classification"] == "full-cover"
    assert hit["archive_allowed"] is True
    assert hit["lease_id"] == lease["lease_id"]
    assert hit["capsule_id"]
    assert hit["source_updated_at"] == 0
    assert hit["last_refreshed_at"] >= hit["source_updated_at"]
    replay = replay_capsule(db, hit["capsule_id"])
    assert replay["ok"] is True
    assert replay["classification"] == "full-cover"


def test_no_lease_needs_refresh(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        12,
        title="Fix API error",
        files=[{"path": "model_tools.py", "patch_snippet": "APIError"}],
        head_sha="h1",
        base_sha="b1",
    )
    out = search_pr_overlap(
        db,
        REPO,
        files=["model_tools.py"],
        error_messages=["APIError"],
        require_fresh=True,
    )
    assert out["results"][0]["classification"] == "needs-refresh"
    assert out["results"][0]["lease_status"] == "missing"
    assert out["results"][0]["archive_allowed"] is False


def test_historical_overlap_only(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        13,
        files=[{"path": "cli.py", "patch_snippet": "GhostDuplicationError"}],
        head_sha="old",
        base_sha="b",
        source_updated_at=1000,
    )
    upsert_pr_revision(
        db,
        REPO,
        13,
        files=[{"path": "website/docs.md", "patch_snippet": "docs only"}],
        head_sha="new",
        base_sha="b",
        source_updated_at=2000,
    )
    out = search_pr_overlap(
        db, REPO, files=["cli.py"], error_messages=["GhostDuplicationError"], top_k=5
    )
    historical = [
        r for r in out["results"] if r["classification"] == "historical_overlap_only"
    ]
    assert historical
    assert historical[0]["source_updated_at"] == 1000
    assert historical[0]["last_refreshed_at"] is None
    assert all(not r["archive_allowed"] for r in out["results"])


def test_dlq_attempts(tmp_path):
    db = tmp_path / "idx.db"
    record_refresh_failure(db, REPO, 14, "rate_limited", "first")
    rec = record_refresh_failure(db, REPO, 14, "rate_limited", "second")
    assert rec["attempt_count"] == 2
    dlq = get_dead_letters(db, REPO)
    assert dlq[0]["attempt_count"] == 2
    assert dlq[0]["last_error_summary"] == "second"


def test_evidence_lease_status(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        15,
        files=[{"path": "tools/registry.py", "patch_snippet": "register"}],
        head_sha="h",
        base_sha="b",
    )
    assert get_pr_evidence(db, REPO, 15)["lease_status"] == "missing"
    refresh_pr(db, REPO, 15)
    ev = get_pr_evidence(db, REPO, 15)
    assert ev["lease_status"] == "live"
    assert ev["freshness_class"] == "fresh"


def test_replay_capsule(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        16,
        files=[{"path": "agent/cache.py", "patch_snippet": "CacheKeyError"}],
        head_sha="h",
        base_sha="b",
    )
    refresh_pr(db, REPO, 16)
    hit = search_pr_overlap(
        db,
        REPO,
        files=["agent/cache.py"],
        error_messages=["CacheKeyError"],
        require_fresh=True,
    )["results"][0]
    replay = replay_capsule(db, hit["capsule_id"])
    assert replay["payload"]["revision"]["head_sha"] == "h"
    assert replay["payload"]["matched_anchors"]


def test_server_imports_without_mcp(tmp_path, monkeypatch):
    db = tmp_path / "server-health.db"
    monkeypatch.setenv("PR_OVERLAP_DB", str(db))
    spec = importlib.util.spec_from_file_location(
        "pr_overlap_server", PKG / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.health()["ok"] is False
    assert mod.health()["name"] == "pr-overlap-index"
    assert mod.health()["initialized"] is False
    assert mod.health()["schema_version"] is None
    assert "index_version" in mod.health()
    assert not db.exists()


def test_class_api_delegates(tmp_path):
    db = tmp_path / "idx.db"
    idx = PrOverlapIndex(db)
    idx.init_db()
    idx.upsert_pr_revision(
        REPO,
        17,
        title="Fix desktop palette",
        files=[
            {
                "path": "apps/desktop/src/lib/desktop-slash-commands.ts",
                "patch_snippet": "skill command palette",
            }
        ],
        head_sha="h",
        base_sha="b",
    )
    lease = idx.refresh_pr(REPO, 17, ttl_seconds=120)
    assert lease["refresh_status"] == "fresh"
    out = idx.search_pr_overlap(
        REPO,
        issue={
            "title": "Desktop skill command palette",
            "files": ["apps/desktop/src/lib/desktop-slash-commands.ts"],
        },
        require_fresh=True,
    )
    assert out["results"][0]["classification"] == "full-cover"
    assert idx.get_pr_evidence(REPO, 17)["lease_status"] == "live"
    assert (
        idx.replay_capsule(out["results"][0]["capsule_id"])["classification"]
        == "full-cover"
    )


def test_refresh_budget_and_failure_behaviour(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        18,
        files=[{"path": "cli.py", "patch_snippet": "budget"}],
        head_sha="h",
        base_sha="b",
    )
    suggestion = refresh_pr(db, REPO, 18, budget_available=False)
    assert suggestion["refresh_status"] == "suggestion_only"
    assert suggestion["lease_id"] is None
    assert get_pr_evidence(db, REPO, 18)["lease_status"] == "missing"

    failed = refresh_pr(db, REPO, 18, failure_reason="rate_limited")
    assert failed["refresh_status"] == "dead_lettered"
    assert failed["lease_id"] is None
    dlq = get_dead_letters(db, REPO)
    assert dlq[0]["reason_class"] == "rate_limited"
    assert dlq[0]["attempt_count"] == 1


def test_replay_capsule_payload_dict_and_json(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        19,
        files=[{"path": "agent/cache.py", "patch_snippet": "ReplayHashError"}],
        head_sha="h",
        base_sha="b",
    )
    refresh_pr(db, REPO, 19)
    hit = search_pr_overlap(
        db,
        REPO,
        files=["agent/cache.py"],
        error_messages=["ReplayHashError"],
        require_fresh=True,
    )["results"][0]
    stored = replay_capsule(db, hit["capsule_id"])

    from_payload = replay_capsule(db, stored["payload"])
    assert from_payload["classification"] == stored["classification"]
    assert from_payload["ok"] is None

    payload_hash = (
        stored["ok"]
        and hashlib.sha256(
            json.dumps(
                stored["payload"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    payload_json = json.dumps({
        "capsule_id": stored["capsule_id"],
        "capsule_hash": payload_hash,
        "payload": stored["payload"],
    })
    from_json = replay_capsule(db, payload_json)
    assert from_json["classification"] == stored["classification"]
    assert from_json["ok"] is True


def _jsonrpc(proc, message):
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, proc.stderr.read()
    return json.loads(line)


def test_mcp_stdio_server_lists_tools_and_calls_health(tmp_path):
    env = dict(
        os.environ, PR_OVERLAP_DB=str(tmp_path / "stdio.db"), PYTHONPATH=str(PKG)
    )
    proc = subprocess.Popen(
        [sys.executable, str(PKG / "server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        init = _jsonrpc(
            proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert init["result"]["serverInfo"]["name"] == "pr-overlap-index"
        assert "tools" in init["result"]["capabilities"]

        listed = _jsonrpc(
            proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {"search_pr_overlap", "get_pr_evidence", "refresh_pr", "health"} <= names

        health_result = _jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "health", "arguments": {}},
            },
        )
        payload = json.loads(health_result["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert payload["initialized"] is False
        assert payload["db_path"] == str(tmp_path / "stdio.db")
        assert payload["schema_version"] is None
        assert not (tmp_path / "stdio.db").exists()

        ping = _jsonrpc(
            proc, {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}}
        )
        assert ping["result"] == {}

        unknown = _jsonrpc(
            proc, {"jsonrpc": "2.0", "id": 5, "method": "missing", "params": {}}
        )
        assert unknown["error"]["code"] == -32601
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_query_and_health_do_not_create_missing_db(tmp_path):
    db = tmp_path / "missing.db"
    snapshot = health_snapshot(db)
    assert snapshot["ok"] is False
    assert snapshot["initialized"] is False
    assert not db.exists()
    with pytest.raises(IndexNotInitializedError):
        search_pr_overlap(db, REPO, files=["cli.py"])
    assert not db.exists()


def test_mcp_search_query_does_not_create_db_when_index_missing(tmp_path):
    db = tmp_path / "missing-stdio.db"
    env = dict(os.environ, PR_OVERLAP_DB=str(db), PYTHONPATH=str(PKG))
    proc = subprocess.Popen(
        [sys.executable, str(PKG / "server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        result = _jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "search_pr_overlap",
                    "arguments": {"repo": REPO, "files": ["cli.py"]},
                },
            },
        )
        assert result["error"]["code"] == -32000
        assert "not initialized" in result["error"]["message"]
        assert not db.exists()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_mcp_refresh_rejects_revision_mutation_by_default(tmp_path, monkeypatch):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        30,
        files=[{"path": "original.py", "patch_snippet": "ORIGINAL"}],
        head_sha="h1",
        base_sha="b1",
    )
    monkeypatch.setenv("PR_OVERLAP_DB", str(db))
    monkeypatch.delenv("PR_OVERLAP_ENABLE_ADMIN_MUTATION", raising=False)
    spec = importlib.util.spec_from_file_location(
        "pr_overlap_server_reject", PKG / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    payload = mod._call_tool(
        "refresh_pr",
        {
            "repo": REPO,
            "pr_number": 30,
            "head_sha": "h2",
            "base_sha": "b2",
            "title": "mutated",
            "files": [{"path": "mutated.py", "patch_snippet": "MUTATED"}],
        },
    )
    body = json.loads(payload["content"][0]["text"])
    assert body["refresh_status"] == "rejected"
    assert set(body["rejected_fields"]) >= {"head_sha", "base_sha", "title", "files"}

    ev = get_pr_evidence(db, REPO, 30)
    assert ev["revision"]["head_sha"] == "h1"
    assert [f["path"] for f in ev["files"]] == ["original.py"]


def test_mcp_public_refresh_cannot_mint_archive_lease(tmp_path, monkeypatch):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        34,
        title="Fix public refresh bypass",
        files=[{"path": "bypass.py", "patch_snippet": "PublicRefreshBypass"}],
        head_sha="h1",
        base_sha="b1",
    )
    monkeypatch.setenv("PR_OVERLAP_DB", str(db))
    monkeypatch.delenv("PR_OVERLAP_ENABLE_ADMIN_MUTATION", raising=False)
    spec = importlib.util.spec_from_file_location(
        "pr_overlap_server_public_refresh", PKG / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    payload = mod._call_tool(
        "refresh_pr",
        {"repo": REPO, "pr_number": 34},
    )
    body = json.loads(payload["content"][0]["text"])
    assert body["refresh_status"] == "suggestion_only"
    assert body["lease_id"] is None

    hit = search_pr_overlap(
        db,
        REPO,
        files=["bypass.py"],
        error_messages=["PublicRefreshBypass"],
        require_fresh=True,
    )["results"][0]
    assert hit["classification"] == "needs-refresh"
    assert hit["archive_allowed"] is False
    assert hit["lease_status"] == "missing"


def test_require_fresh_budget_false_surfaces_suggestion_only(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        20,
        files=[{"path": "gateway/run.py", "patch_snippet": "QueueOverflow"}],
        head_sha="h",
        base_sha="b",
    )
    out = search_pr_overlap(
        db,
        REPO,
        files=["gateway/run.py"],
        error_messages=["QueueOverflow"],
        require_fresh=True,
        refresh_budget_available=False,
    )
    hit = out["results"][0]
    assert hit["classification"] == "needs-refresh"
    assert hit["refresh_status"] == "suggestion_only"
    assert hit["archive_allowed"] is False
    assert "dead_letter" not in hit
    assert get_dead_letters(db, REPO) == []


def test_require_fresh_failure_reason_records_dlq(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        21,
        files=[{"path": "run_agent.py", "patch_snippet": "BudgetExceeded"}],
        head_sha="h",
        base_sha="b",
    )
    out = search_pr_overlap(
        db,
        REPO,
        files=["run_agent.py"],
        error_messages=["BudgetExceeded"],
        require_fresh=True,
        refresh_failure_reason="rate_limited",
    )
    hit = out["results"][0]
    assert hit["classification"] == "needs-refresh"
    assert hit["refresh_status"] == "dead_lettered"
    assert hit["archive_allowed"] is False
    assert hit["dead_letter"]["reason_class"] == "rate_limited"
    assert hit["dead_letter"]["next_retry_at"] >= hit["dead_letter"]["last_failed_at"]
    dlq = get_dead_letters(db, REPO)
    assert dlq[0]["reason_class"] == "rate_limited"
    assert dlq[0]["attempt_count"] == 1


def test_health_snapshot_counts_index_state(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        22,
        files=[{"path": "cli.py", "patch_snippet": "health old"}],
        head_sha="h1",
        base_sha="b",
    )
    upsert_pr_revision(
        db,
        REPO,
        22,
        files=[{"path": "cli.py", "patch_snippet": "health new"}],
        head_sha="h2",
        base_sha="b",
    )
    refresh_pr(db, REPO, 22)
    record_refresh_failure(db, REPO, 22, "rate_limited", "health")

    snapshot = health_snapshot(db)

    assert snapshot["ok"] is True
    assert snapshot["schema_version"] == 3
    assert snapshot["index_version"]["score"] == "lexical-v1"
    assert snapshot["indexed_pr_count"] == 1
    assert snapshot["latest_live_manifest_count"] == 1
    assert snapshot["tombstone_count"] == 1
    assert snapshot["active_lease_count"] == 1
    assert snapshot["dead_letter_count"] == 1
    assert snapshot["last_sync_at"]
    assert snapshot["last_refreshed_at"]
    assert "coverage_by_scope" in snapshot
    assert "db_bytes" in snapshot
    assert "wal_bytes" in snapshot
    assert "brownout_state" in snapshot


def test_refresh_revision_mutation_rolls_back_when_lease_insert_fails(
    tmp_path, monkeypatch
):
    import pr_overlap_index.core as core

    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        31,
        files=[{"path": "old.py", "patch_snippet": "OldAnchor"}],
        head_sha="h1",
        base_sha="b1",
    )

    class FixedUUID:
        hex = "fixedlease"

    monkeypatch.setattr(core.uuid, "uuid4", lambda: FixedUUID())
    refresh_pr(
        db,
        REPO,
        31,
        files=[{"path": "new.py", "patch_snippet": "NewAnchor"}],
        head_sha="h2",
        base_sha="b1",
    )
    with pytest.raises(Exception):
        refresh_pr(
            db,
            REPO,
            31,
            files=[{"path": "broken.py", "patch_snippet": "BrokenAnchor"}],
            head_sha="h3",
            base_sha="b1",
        )
    ev = get_pr_evidence(db, REPO, 31)
    assert ev["revision"]["head_sha"] == "h2"
    assert [f["path"] for f in ev["files"]] == ["new.py"]
    out = search_pr_overlap(db, REPO, files=["broken.py"], top_k=5)
    assert out["results"] == []


def test_refresh_revision_mutation_lease_matches_new_revision(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        32,
        files=[{"path": "old.py", "patch_snippet": "OldOnlyAnchor"}],
        head_sha="h1",
        base_sha="b1",
    )
    lease = refresh_pr(
        db,
        REPO,
        32,
        files=[{"path": "new.py", "patch_snippet": "NewOnlyAnchor"}],
        head_sha="h2",
        base_sha="b1",
    )
    ev = get_pr_evidence(db, REPO, 32)
    assert lease["head_sha"] == "h2"
    assert ev["revision"]["head_sha"] == lease["head_sha"]
    hit = search_pr_overlap(
        db,
        REPO,
        files=["new.py"],
        error_messages=["NewOnlyAnchor"],
        require_fresh=True,
    )["results"][0]
    assert hit["classification"] == "full-cover"
    historical = search_pr_overlap(
        db, REPO, files=["old.py"], error_messages=["OldOnlyAnchor"]
    )["results"][0]
    assert historical["classification"] == "historical_overlap_only"


def test_capsule_gc_deletes_capsules_but_preserves_historical_overlap_anchors(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        33,
        files=[{"path": "old.py", "patch_snippet": "OldOnlyAnchor"}],
        head_sha="h1",
        base_sha="b1",
        source_updated_at=1000,
    )
    refresh_pr(db, REPO, 33)
    hit = search_pr_overlap(
        db,
        REPO,
        files=["old.py"],
        error_messages=["OldOnlyAnchor"],
        require_fresh=True,
    )["results"][0]
    capsule_id = hit["capsule_id"]
    upsert_pr_revision(
        db,
        REPO,
        33,
        files=[{"path": "new.py", "patch_snippet": "NewOnlyAnchor"}],
        head_sha="h2",
        base_sha="b1",
        source_updated_at=2000,
    )

    gc = gc_replay_capsules(db, older_than=9999999999)
    assert gc["deleted"] >= 1
    with pytest.raises(KeyError):
        replay_capsule(db, capsule_id)
    historical = search_pr_overlap(
        db, REPO, files=["old.py"], error_messages=["OldOnlyAnchor"], top_k=5
    )["results"][0]
    assert historical["classification"] == "historical_overlap_only"
    assert historical["archive_allowed"] is False


class FakePRClient:
    def __init__(self, prs):
        self.prs = prs

    def iter_prs(self, repo, *, scope="open", since=None):
        yield from self.prs


class ResumeAwarePRClient:
    def __init__(self, prs):
        self.prs = prs
        self.calls = []

    def iter_prs(self, repo, *, scope="open", since=None, skip_keys=None):
        skip_keys = set(skip_keys or [])
        self.calls.append({
            "scope": scope,
            "since": since,
            "skip_keys": set(skip_keys),
        })
        for pr in self.prs:
            updated_at = str(pr.get("source_updated_at_raw") or "")
            if since and updated_at <= since:
                return
            source = (
                pr.get("source_updated_at_raw")
                or pr.get("source_updated_at")
                or pr.get("head_sha")
                or ""
            )
            key = f"{pr.get('number')}:{source}"
            if key in skip_keys:
                continue
            yield pr


def test_one_shot_indexer_stops_at_request_cap(tmp_path):
    db = tmp_path / "idx.db"
    prs = [
        {
            "number": i,
            "title": f"PR {i}",
            "head_sha": f"h{i}",
            "base_sha": "b",
            "files": [{"path": f"f{i}.py", "patch": "Anchor"}],
        }
        for i in range(40, 45)
    ]
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient(prs),
        max_requests=2,
        min_free_disk_bytes=0,
    )
    assert summary["capped"] is True
    assert summary["stop_reason"] == "request_cap"
    assert summary["requests_used"] == 2
    assert health_snapshot(db)["indexed_pr_count"] == 2


def test_one_shot_indexer_defaults_are_vps_safe():
    assert DEFAULT_MAX_RUNTIME_SECONDS <= 45
    assert DEFAULT_MAX_RSS_MB <= 250


def test_one_shot_indexer_capped_runs_resume_forward(tmp_path):
    db = tmp_path / "idx.db"
    prs = [
        {
            "number": i,
            "title": f"PR {i}",
            "head_sha": f"h{i}",
            "base_sha": "b",
            "source_updated_at": float(i),
            "source_updated_at_raw": f"2026-07-0{95 - i}T00:00:00Z",
            "files": [{"path": f"f{i}.py", "patch": f"Anchor{i}"}],
        }
        for i in range(91, 95)
    ]
    first_client = ResumeAwarePRClient(prs)
    first = run_one_shot_indexer(
        db,
        REPO,
        client=first_client,
        batch_limit=2,
        min_free_disk_bytes=0,
    )
    assert first["capped"] is True
    assert first["stop_reason"] == "batch_cap"
    assert first["indexed"] == 2
    assert first["resume_hint"]["processed_keys"] == 2

    second_client = ResumeAwarePRClient(prs)
    second = run_one_shot_indexer(
        db,
        REPO,
        client=second_client,
        batch_limit=10,
        min_free_disk_bytes=0,
    )
    assert len(second_client.calls[0]["skip_keys"]) == 2
    assert second["indexed"] == 2
    assert second["capped"] is False
    assert second["resume_hint"]["processed_keys"] == 0
    assert second["resume_hint"]["since"] == "2026-07-04T00:00:00Z"
    assert health_snapshot(db)["indexed_pr_count"] == 4

    third_client = ResumeAwarePRClient(prs)
    third = run_one_shot_indexer(
        db,
        REPO,
        client=third_client,
        batch_limit=10,
        min_free_disk_bytes=0,
    )
    assert third_client.calls[0]["since"] == "2026-07-04T00:00:00Z"
    assert third["indexed"] == 0


def test_indexer_watermarks_are_per_scope(tmp_path):
    db = tmp_path / "idx.db"
    run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 46,
                "head_sha": "h46",
                "base_sha": "b",
                "source_updated_at": 46.0,
                "files": [],
            }
        ]),
        scope="open",
        min_free_disk_bytes=0,
    )
    run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 47,
                "head_sha": "h47",
                "base_sha": "b",
                "source_updated_at": 47.0,
                "files": [],
            }
        ]),
        scope="recent",
        min_free_disk_bytes=0,
    )
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT scope, watermark FROM indexer_watermarks ORDER BY scope"
    ).fetchall()
    conn.close()
    assert [row[0] for row in rows] == ["hot_open", "recent_closed"]
    assert all(json.loads(row[1])["schema_version"] == 1 for row in rows)


def test_one_shot_indexer_enforces_file_and_patch_byte_caps(tmp_path):
    db = tmp_path / "idx.db"
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 50,
                "title": "caps",
                "head_sha": "h",
                "base_sha": "b",
                "files": [
                    {"path": "a.py", "patch": "A" * 100},
                    {"path": "b.py", "patch": "B" * 100},
                    {"path": "c.py", "patch": "C" * 100},
                ],
            }
        ]),
        max_files_per_pr=2,
        max_patch_bytes=16,
        max_total_patch_bytes=32,
        min_free_disk_bytes=0,
    )
    assert summary["indexed"] == 1
    assert summary["truncated_files"] == 2
    assert summary["skipped_files"] == 1
    ev = get_pr_evidence(db, REPO, 50)
    assert len(ev["files"]) == 2
    assert all(len(f["patch_snippet"].encode()) <= 16 for f in ev["files"])
    assert all(f["patch_hash"] for f in ev["files"])
    assert all(f["hunk_hash"] for f in ev["files"])


def test_one_shot_indexer_refuses_overlapping_lock(tmp_path):
    db = tmp_path / "idx.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO indexer_locks(name,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?)",
        (f"indexer:{REPO}", "other", 9999999999, 9999999999),
    )
    conn.commit()
    conn.close()
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {"number": 60, "head_sha": "h", "base_sha": "b", "files": []}
        ]),
        min_free_disk_bytes=0,
    )
    assert summary["capped"] is True
    assert summary["stop_reason"] == "lock_held"
    assert health_snapshot(db)["indexed_pr_count"] == 0


class CountingGitHubClient(GitHubRestClient):
    def __init__(self):
        super().__init__(token=None)

    def _json(self, url):
        if self.max_requests is not None and self.requests_used >= self.max_requests:
            raise RequestBudgetExceeded("budget exhausted")
        self.requests_used += 1
        if url.endswith("/files"):
            return [{"filename": "a.py", "patch": "Anchor"}]
        return [
            {
                "number": 70,
                "title": "budget",
                "body": "",
                "state": "open",
                "head": {"sha": "h"},
                "base": {"sha": "b"},
                "updated_at": "2026-07-01T12:34:56Z",
                "url": "https://api.github.example/repos/o/r/pulls/70",
                "html_url": "https://github.example/o/r/pull/70",
            }
        ]


class MissingFilesGitHubClient(GitHubRestClient):
    def __init__(self):
        super().__init__(token=None)

    def _json(self, url):
        if self.max_requests is not None and self.requests_used >= self.max_requests:
            raise RequestBudgetExceeded("budget exhausted")
        self.requests_used += 1
        if url.endswith("/files"):
            raise GitHubNotFoundError("GitHub HTTP 404: files not found")
        return [
            {
                "number": 72,
                "title": "metadata only",
                "body": "Still useful for overlap by title and body",
                "state": "open",
                "head": {"sha": "h72"},
                "base": {"sha": "b"},
                "updated_at": "2026-07-01T12:34:56Z",
                "url": "https://api.github.example/repos/o/r/pulls/72",
                "html_url": "https://github.example/o/r/pull/72",
            }
        ]


def test_github_rest_client_request_budget_counts_page_and_files_requests(tmp_path):
    db = tmp_path / "idx.db"
    client = CountingGitHubClient()
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=client,
        max_requests=1,
        min_free_disk_bytes=0,
    )
    assert summary["capped"] is True
    assert summary["stop_reason"] == "request_cap"
    assert summary["requests_used"] == 1
    assert health_snapshot(db)["indexed_pr_count"] == 0


def test_parse_github_timestamp_and_scope_aliases():
    assert parse_github_timestamp("2026-07-01T12:34:56Z") == pytest.approx(
        1782909296.0
    )
    assert parse_github_timestamp(None) == 0
    assert normalize_scope("open") == "hot_open"
    assert normalize_scope("recent") == "recent_closed"
    assert normalize_scope("closed") == "recent_closed"
    assert normalize_scope("cold_archive") == "cold_archive"


def test_github_rest_client_stores_upstream_updated_at(tmp_path):
    db = tmp_path / "idx.db"
    client = CountingGitHubClient()
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=client,
        max_requests=3,
        min_free_disk_bytes=0,
    )
    assert summary["indexed"] == 1
    assert summary["scope"] == "hot_open"
    ev = get_pr_evidence(db, REPO, 70)
    assert ev["source_updated_at"] == pytest.approx(1782909296.0)


def test_github_rest_client_keeps_pr_metadata_when_files_endpoint_404(tmp_path):
    db = tmp_path / "idx.db"
    client = MissingFilesGitHubClient()
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=client,
        max_requests=3,
        min_free_disk_bytes=0,
    )

    assert summary["indexed"] == 1
    assert summary["file_fetch_errors"] == 1
    ev = get_pr_evidence(db, REPO, 72)
    assert ev["title"] == "metadata only"
    assert ev["files"] == []


def test_indexer_preserves_unknown_upstream_updated_at(tmp_path):
    db = tmp_path / "idx.db"
    run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 71,
                "title": "unknown timestamp",
                "head_sha": "h",
                "base_sha": "b",
                "files": [{"path": "unknown.py", "patch": "UnknownTimestamp"}],
            }
        ]),
        min_free_disk_bytes=0,
    )
    ev = get_pr_evidence(db, REPO, 71)
    assert ev["source_updated_at"] == 0


def test_indexer_records_run_and_scope_coverage(tmp_path):
    db = tmp_path / "idx.db"
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 80,
                "title": "coverage",
                "head_sha": "h",
                "base_sha": "b",
                "source_updated_at": 1234.0,
                "files": [{"path": "coverage.py", "patch": "CoverageAnchor"}],
            }
        ]),
        scope="recent",
        max_requests=5,
        min_free_disk_bytes=0,
    )
    assert summary["scope"] == "recent_closed"
    assert summary["profile"] == "recent_closed"
    assert summary["run_id"]
    snapshot = health_snapshot(db)
    coverage = snapshot["coverage_by_scope"]["recent_closed"]
    assert coverage["coverage_count"] == 1
    assert coverage["coverage_window"]["newest_source_updated_at"] == 1234.0
    assert coverage["coverage_denominator_kind"] == "unknown"
    run = snapshot["last_run_by_scope"]["recent_closed"]
    assert run["indexed"] == 1
    assert run["stop_reason"] == "complete"
    assert isinstance(run["db_bytes"], int)
    assert isinstance(run["wal_bytes"], int)


def test_cold_archive_coverage_is_offline_until_gate_passes(tmp_path):
    db = tmp_path / "idx.db"
    run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 85,
                "title": "cold coverage",
                "head_sha": "h",
                "base_sha": "b",
                "source_updated_at": 85.0,
                "files": [{"path": "cold.py", "patch": "ColdCoverage"}],
            }
        ]),
        scope="cold_archive",
        min_free_disk_bytes=0,
    )
    snapshot = health_snapshot(db)
    cold = snapshot["coverage_by_scope"]["cold_archive"]
    assert cold["serving_active"] is False
    assert cold["serving_gate"] == "cold_scale_gate_required"
    assert snapshot["archive_confidence_floor"] == "unknown"
    out = search_pr_overlap(
        db,
        REPO,
        files=["cold.py"],
        error_messages=["ColdCoverage"],
        top_k=5,
    )
    assert out["results"] == []


def test_lock_held_records_run_summary(tmp_path):
    db = tmp_path / "idx.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO indexer_locks(name,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?)",
        (f"indexer:{REPO}", "other", 9999999999, 9999999999),
    )
    conn.commit()
    conn.close()
    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {"number": 81, "head_sha": "h", "base_sha": "b", "files": []}
        ]),
        min_free_disk_bytes=0,
    )
    assert summary["stop_reason"] == "lock_held"
    run = health_snapshot(db)["last_run_by_scope"]["hot_open"]
    assert run["stop_reason"] == "lock_held"
    assert run["indexed"] == 0


def test_search_reports_candidate_prefilter(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        82,
        title="prefilter",
        files=[{"path": "prefilter.py", "patch_snippet": "PrefilterAnchor"}],
        head_sha="h",
        base_sha="b",
    )
    out = search_pr_overlap(
        db, REPO, files=["prefilter.py"], error_messages=["PrefilterAnchor"]
    )
    assert out["results"]
    assert out["candidate_prefilter"]["enabled"] is True
    assert out["candidate_prefilter"]["reason"] == "term_match"


def test_search_prefilter_falls_back_when_term_index_is_incomplete(tmp_path):
    db = tmp_path / "idx.db"
    old_manifest = upsert_pr_revision(
        db,
        REPO,
        83,
        title="old candidate",
        files=[{"path": "old.py", "patch_snippet": "OldAnchor"}],
        head_sha="old",
        base_sha="b",
    )
    upsert_pr_revision(
        db,
        REPO,
        84,
        title="new candidate",
        files=[{"path": "new.py", "patch_snippet": "NewAnchor"}],
        head_sha="new",
        base_sha="b",
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "DELETE FROM pr_revision_terms WHERE manifest_id=?",
        (old_manifest["manifest_id"],),
    )
    conn.commit()
    conn.close()

    out = search_pr_overlap(
        db,
        REPO,
        error_messages=["OldAnchor", "NewAnchor"],
        top_k=10,
    )

    assert out["candidate_prefilter"]["enabled"] is False
    assert out["candidate_prefilter"]["reason"] == "incomplete_term_index"
    assert {r["pr_number"] for r in out["results"]} >= {83, 84}


def test_search_prefilter_disables_for_large_term_sets_to_preserve_recall(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        86,
        title="early term",
        files=[{"path": "early.py", "patch_snippet": "a000"}],
        head_sha="h1",
        base_sha="b",
    )
    upsert_pr_revision(
        db,
        REPO,
        87,
        title="late term",
        files=[{"path": "late.py", "patch_snippet": "z999"}],
        head_sha="h2",
        base_sha="b",
    )
    terms = [f"a{i:03d}" for i in range(80)] + ["z999"]

    out = search_pr_overlap(db, REPO, error_messages=terms, top_k=10)

    assert out["candidate_prefilter"]["enabled"] is False
    assert out["candidate_prefilter"]["reason"] == "too_many_terms"
    assert {r["pr_number"] for r in out["results"]} >= {86, 87}


def test_search_and_health_stay_fast_on_5k_local_fixture(tmp_path):
    db = tmp_path / "idx.db"
    init_db(db)
    for i in range(5000):
        upsert_pr_revision(
            db,
            REPO,
            i + 1000,
            title=f"PR {i}",
            files=[
                {
                    "path": f"pkg/file_{i % 50}.py",
                    "patch_snippet": f"Anchor{i} sharedtoken",
                }
            ],
            head_sha=f"h{i}",
            base_sha="b",
            index_scope="hot_open",
        )

    started = time.perf_counter()
    out = search_pr_overlap(
        db,
        REPO,
        files=["pkg/file_42.py"],
        error_messages=["Anchor4242"],
        top_k=5,
    )
    query_seconds = time.perf_counter() - started
    started = time.perf_counter()
    snapshot = health_snapshot(db)
    health_seconds = time.perf_counter() - started

    assert out["results"]
    assert out["candidate_prefilter"]["enabled"] is True
    assert snapshot["indexed_pr_count"] == 5000
    assert query_seconds < 1.0
    assert health_seconds < 0.5


def test_import_disk_preflight_reports_insufficient_disk(tmp_path):
    live = tmp_path / "live.db"
    staging = tmp_path / "staging.db"
    init_db(live)
    init_db(staging)
    preflight = import_disk_preflight(
        live,
        staging,
        min_free_disk_bytes=10**18,
    )
    assert preflight["ok"] is False
    assert preflight["reason"] == "insufficient_disk"
    assert preflight["required_free_disk_bytes"] > preflight["free_disk_bytes"]


def test_validate_staging_import_db(tmp_path):
    missing = validate_staging_import_db(tmp_path / "missing.db")
    assert missing["ok"] is False
    assert missing["reason"] == "missing_staging_db"

    staging = tmp_path / "staging.db"
    init_db(staging)
    valid = validate_staging_import_db(staging)
    assert valid["ok"] is True
    assert valid["schema_version"] == 3


def test_cold_import_bundle_activate_and_rollback(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    upsert_pr_revision(
        live,
        REPO,
        90,
        title="live",
        files=[{"path": "live.py", "patch_snippet": "LiveOnly"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        91,
        title="cold",
        files=[{"path": "cold.py", "patch_snippet": "ColdOnly"}],
        head_sha="cold",
        base_sha="b",
        index_scope="cold_archive",
    )
    bundle = tmp_path / "bundle"
    exported = export_cold_import_bundle(cold, bundle, repo=REPO)
    assert exported["ok"] is True
    validation = validate_cold_import_bundle(bundle, expected_repo=REPO)
    assert validation["ok"] is True

    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is True
    assert activated["rollback_db"]
    assert not pathlib.Path(f"{live}.import.lock").exists()
    assert get_pr_evidence(live, REPO, 91)["revision"]["head_sha"] == "cold"
    with pytest.raises(KeyError):
        get_pr_evidence(live, REPO, 90)

    restored = restore_import_backup(live, activated["rollback_db"])
    assert restored["ok"] is True
    assert get_pr_evidence(live, REPO, 90)["revision"]["head_sha"] == "live"
    with pytest.raises(KeyError):
        get_pr_evidence(live, REPO, 91)


def test_offline_backfill_loop_resumes_and_exports_bundle(tmp_path):
    db = tmp_path / "builder.db"
    bundle = tmp_path / "builder-bundle"
    prs = [
        {
            "number": i,
            "title": f"offline {i}",
            "head_sha": f"h{i}",
            "base_sha": "b",
            "source_updated_at": float(i),
            "source_updated_at_raw": f"2026-07-07T00:0{i - 130}:00Z",
            "files": [{"path": f"offline_{i}.py", "patch": f"OfflineAnchor{i}"}],
        }
        for i in range(130, 134)
    ]

    result = run_backfill_loop(
        db,
        REPO,
        client=ResumeAwarePRClient(prs),
        max_passes=5,
        batch_limit=2,
        max_requests=20,
        min_free_disk_bytes=0,
        bundle_dir=bundle,
    )

    assert result["ok"] is True
    assert result["stop_reason"] == "index_complete"
    assert result["indexed_total"] == 4
    assert result["passes"] == 2
    assert result["export"]["ok"] is True
    assert validate_cold_import_bundle(bundle, expected_repo=REPO)["ok"] is True
    assert health_snapshot(db)["indexed_pr_count"] == 4


def test_offline_backfill_reconnects_after_transient_error(tmp_path):
    class ReconnectingClient:
        def __init__(self):
            self.calls = 0

        def iter_prs(self, repo, *, scope="open", since=None, skip_keys=None):
            self.calls += 1
            skip_keys = set(skip_keys or [])
            prs = [
                {
                    "number": 140,
                    "title": "partial export",
                    "head_sha": "h140",
                    "base_sha": "b",
                    "source_updated_at": 140.0,
                    "source_updated_at_raw": "2026-07-07T14:00:00Z",
                    "files": [{"path": "partial.py", "patch": "PartialAnchor"}],
                },
                {
                    "number": 141,
                    "title": "resumed export",
                    "head_sha": "h141",
                    "base_sha": "b",
                    "source_updated_at": 141.0,
                    "source_updated_at_raw": "2026-07-07T14:01:00Z",
                    "files": [{"path": "resumed.py", "patch": "ResumedAnchor"}],
                },
            ]
            for index, pr in enumerate(prs):
                key = f"{pr['number']}:{pr['source_updated_at_raw']}"
                if key in skip_keys:
                    continue
                yield pr
                if self.calls == 1 and index == 0:
                    raise TransientGitHubError(
                        "GitHub transient error after retries: RemoteDisconnected"
                    )

    db = tmp_path / "partial.db"
    bundle = tmp_path / "partial-bundle"
    sleeps = []
    client = ReconnectingClient()
    result = run_backfill_loop(
        db,
        REPO,
        client=client,
        max_passes=3,
        batch_limit=5,
        max_requests=20,
        min_free_disk_bytes=0,
        bundle_dir=bundle,
        reconnect_base_sleep=1,
        reconnect_max_sleep=10,
        sleep_fn=sleeps.append,
    )

    assert result["ok"] is True
    assert result["stop_reason"] == "index_complete"
    assert result["indexed_total"] == 2
    assert result["errors"][0]["type"] == "TransientGitHubError"
    assert result["errors"][0]["sleep_seconds"] == 1
    assert sleeps == [1]
    assert client.calls == 2
    assert result["export"]["ok"] is True
    assert result["interim_exports"][0]["reason"] == "transient_network"
    assert validate_cold_import_bundle(bundle, expected_repo=REPO)["ok"] is True
    assert health_snapshot(db)["indexed_pr_count"] == 2


def test_offline_backfill_stops_after_reconnect_limit_with_bundle(tmp_path):
    class AlwaysFailingClient:
        def iter_prs(self, repo, *, scope="open", since=None, skip_keys=None):
            pr = {
                "number": 142,
                "title": "partial limit export",
                "head_sha": "h142",
                "base_sha": "b",
                "source_updated_at": 142.0,
                "source_updated_at_raw": "2026-07-07T14:02:00Z",
                "files": [{"path": "partial_limit.py", "patch": "PartialLimitAnchor"}],
            }
            if f"{pr['number']}:{pr['source_updated_at_raw']}" not in set(skip_keys or []):
                yield pr
            raise TransientGitHubError(
                "GitHub transient error after retries: RemoteDisconnected"
            )

    db = tmp_path / "partial-limit.db"
    bundle = tmp_path / "partial-limit-bundle"
    sleeps = []
    result = run_backfill_loop(
        db,
        REPO,
        client=AlwaysFailingClient(),
        max_passes=5,
        batch_limit=5,
        max_requests=20,
        min_free_disk_bytes=0,
        bundle_dir=bundle,
        reconnect_limit=1,
        reconnect_base_sleep=2,
        reconnect_max_sleep=10,
        sleep_fn=sleeps.append,
    )

    assert result["ok"] is False
    assert result["stop_reason"] == "transient_error"
    assert result["indexed_total"] == 1
    assert result["reconnect_failures"] == 2
    assert sleeps == [2]
    assert result["export"]["ok"] is True
    assert validate_cold_import_bundle(bundle, expected_repo=REPO)["ok"] is True
    assert health_snapshot(db)["indexed_pr_count"] == 1


def test_offline_backfill_cli_export_and_validate(tmp_path, capsys):
    db = tmp_path / "cli-export.db"
    bundle = tmp_path / "cli-bundle"
    upsert_pr_revision(
        db,
        REPO,
        129,
        title="cli export",
        files=[{"path": "cli_export.py", "patch_snippet": "CliExportAnchor"}],
        head_sha="h",
        base_sha="b",
    )

    assert offline_backfill_main([
        "export-bundle",
        REPO,
        "--db",
        str(db),
        "--bundle-dir",
        str(bundle),
    ]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["ok"] is True

    assert offline_backfill_main([
        "validate-bundle",
        "--bundle-dir",
        str(bundle),
        "--expected-repo",
        REPO,
    ]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["ok"] is True


def test_restore_import_backup_rejects_lockfile_and_active_db_lock(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    upsert_pr_revision(
        live,
        REPO,
        120,
        title="restore live",
        files=[{"path": "live.py", "patch_snippet": "RestoreLive"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        121,
        title="restore cold",
        files=[{"path": "cold.py", "patch_snippet": "RestoreCold"}],
        head_sha="cold",
        base_sha="b",
    )
    bundle = tmp_path / "bundle-restore-lock"
    assert export_cold_import_bundle(cold, bundle, repo=REPO)["ok"] is True
    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is True

    lockfile = pathlib.Path(f"{live}.import.lock")
    lockfile.write_text("old but active", encoding="utf-8")
    os.utime(lockfile, (time.time() - 3600, time.time() - 3600))
    blocked = restore_import_backup(live, activated["rollback_db"])
    assert blocked["ok"] is False
    assert blocked["reason"] == "import_lock_held"
    assert get_pr_evidence(live, REPO, 121)["revision"]["head_sha"] == "cold"
    lockfile.unlink()

    conn = sqlite3.connect(live)
    conn.execute(
        "INSERT INTO indexer_locks(name,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?)",
        (f"indexer:{REPO}", "indexer", time.time(), time.time()),
    )
    conn.commit()
    conn.close()
    blocked = restore_import_backup(live, activated["rollback_db"])
    assert blocked["ok"] is False
    assert blocked["reason"] == "import_lock_held"
    assert get_pr_evidence(live, REPO, 121)["revision"]["head_sha"] == "cold"


def test_cold_import_manifest_failure_leaves_live_db_unchanged(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    upsert_pr_revision(
        live,
        REPO,
        92,
        title="live unchanged",
        files=[{"path": "live.py", "patch_snippet": "Unchanged"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        93,
        title="cold invalid",
        files=[{"path": "cold.py", "patch_snippet": "Invalid"}],
        head_sha="cold",
        base_sha="b",
    )
    bundle = tmp_path / "bundle"
    assert export_cold_import_bundle(cold, bundle, repo=REPO)["ok"] is True
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["row_counts"]["prs"] += 1
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is False
    assert activated["reason"] == "bundle_validation_failed"
    assert get_pr_evidence(live, REPO, 92)["revision"]["head_sha"] == "live"
    with pytest.raises(KeyError):
        get_pr_evidence(live, REPO, 93)


def test_cold_import_rejects_bad_bundle_schema(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    upsert_pr_revision(
        live,
        REPO,
        94,
        title="live schema",
        files=[{"path": "live.py", "patch_snippet": "LiveSchema"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        95,
        title="cold schema",
        files=[{"path": "cold.py", "patch_snippet": "ColdSchema"}],
        head_sha="cold",
        base_sha="b",
    )
    bundle = tmp_path / "bundle-schema"
    assert export_cold_import_bundle(cold, bundle, repo=REPO)["ok"] is True
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["bundle_schema_version"] = 999
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    validation = validate_cold_import_bundle(bundle, expected_repo=REPO)
    assert validation["ok"] is False
    assert validation["reason"] == "unsupported_bundle_schema"
    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is False
    assert activated["reason"] == "bundle_validation_failed"
    assert get_pr_evidence(live, REPO, 94)["revision"]["head_sha"] == "live"


def test_cold_import_rejects_artifact_path_outside_bundle(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    outside = tmp_path / "outside.db"
    upsert_pr_revision(
        live,
        REPO,
        122,
        title="live artifact path",
        files=[{"path": "live.py", "patch_snippet": "LiveArtifactPath"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        123,
        title="cold artifact path",
        files=[{"path": "cold.py", "patch_snippet": "ColdArtifactPath"}],
        head_sha="cold",
        base_sha="b",
    )
    init_db(outside)
    bundle = tmp_path / "bundle-path"
    assert export_cold_import_bundle(cold, bundle, repo=REPO)["ok"] is True
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifact"] = "../outside.db"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    validation = validate_cold_import_bundle(bundle, expected_repo=REPO)
    assert validation["ok"] is False
    assert validation["reason"] == "artifact_outside_bundle"
    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is False
    assert activated["reason"] == "bundle_validation_failed"
    assert get_pr_evidence(live, REPO, 122)["revision"]["head_sha"] == "live"


def test_cold_import_invalid_bundle_does_not_create_missing_live_db(tmp_path):
    live = tmp_path / "missing-live.db"
    bundle = tmp_path / "invalid-bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")

    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )

    assert activated["ok"] is False
    assert activated["reason"] == "bundle_validation_failed"
    assert not live.exists()
    assert not pathlib.Path(f"{live}.import.lock").exists()


def test_cold_import_lock_rejects_concurrent_indexer(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    upsert_pr_revision(
        live,
        REPO,
        96,
        title="live lock",
        files=[{"path": "live.py", "patch_snippet": "LiveLock"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        97,
        title="cold lock",
        files=[{"path": "cold.py", "patch_snippet": "ColdLock"}],
        head_sha="cold",
        base_sha="b",
    )
    conn = sqlite3.connect(live)
    conn.execute(
        "INSERT INTO indexer_locks(name,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?)",
        (f"indexer:{REPO}", "indexer", time.time(), time.time()),
    )
    conn.commit()
    conn.close()
    bundle = tmp_path / "bundle-lock"
    assert export_cold_import_bundle(cold, bundle, repo=REPO)["ok"] is True

    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is False
    assert activated["reason"] == "import_lock_held"
    assert get_pr_evidence(live, REPO, 96)["revision"]["head_sha"] == "live"


def test_cold_import_lockfile_rejects_activation_and_indexer(tmp_path):
    live = tmp_path / "live.db"
    cold = tmp_path / "cold.db"
    upsert_pr_revision(
        live,
        REPO,
        99,
        title="live lockfile",
        files=[{"path": "live.py", "patch_snippet": "LiveLockfile"}],
        head_sha="live",
        base_sha="b",
    )
    upsert_pr_revision(
        cold,
        REPO,
        100,
        title="cold lockfile",
        files=[{"path": "cold.py", "patch_snippet": "ColdLockfile"}],
        head_sha="cold",
        base_sha="b",
    )
    bundle = tmp_path / "bundle-lockfile"
    assert export_cold_import_bundle(cold, bundle, repo=REPO)["ok"] is True
    lockfile = pathlib.Path(f"{live}.import.lock")
    lockfile.write_text("locked", encoding="utf-8")
    os.utime(lockfile, (time.time() - 3600, time.time() - 3600))

    activated = activate_cold_import_bundle(
        live,
        bundle,
        expected_repo=REPO,
        rollback_dir=tmp_path / "rollback",
        min_free_disk_bytes=0,
    )
    assert activated["ok"] is False
    assert activated["reason"] == "import_lock_held"
    assert "active_lockfile" in activated["detail"]
    assert get_pr_evidence(live, REPO, 99)["revision"]["head_sha"] == "live"

    summary = run_one_shot_indexer(
        live,
        REPO,
        client=FakePRClient([
            {
                "number": 101,
                "title": "blocked by file",
                "head_sha": "blocked",
                "base_sha": "b",
                "files": [],
            }
        ]),
        min_free_disk_bytes=0,
    )
    assert summary["stop_reason"] == "import_lock_held"
    assert health_snapshot(live)["indexed_pr_count"] == 1
    lockfile.unlink()


def test_search_does_not_write_when_import_lockfile_exists(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        124,
        title="locked search capsule",
        files=[{"path": "locked.py", "patch_snippet": "LockedSearchCapsule"}],
        head_sha="locked",
        base_sha="b",
    )
    refresh_pr(db, REPO, 124)
    upsert_pr_revision(
        db,
        REPO,
        125,
        title="locked search deadletter",
        files=[{"path": "dead.py", "patch_snippet": "LockedSearchDeadletter"}],
        head_sha="dead",
        base_sha="b",
    )
    lockfile = pathlib.Path(f"{db}.import.lock")
    lockfile.write_text("locked", encoding="utf-8")
    os.utime(lockfile, (time.time() - 3600, time.time() - 3600))

    hit = search_pr_overlap(
        db,
        REPO,
        files=["locked.py"],
        error_messages=["LockedSearchCapsule"],
        require_fresh=True,
    )["results"][0]
    assert hit["refresh_status"] == "import_lock_held"
    assert hit["archive_allowed"] is False
    assert hit["capsule_id"] is None
    assert health_snapshot(db)["replay_capsule_count"] == 0

    hit = search_pr_overlap(
        db,
        REPO,
        files=["dead.py"],
        error_messages=["LockedSearchDeadletter"],
        require_fresh=True,
        refresh_failure_reason="rate_limited",
    )["results"][0]
    assert hit["refresh_status"] == "import_lock_held"
    assert "dead_letter" not in hit
    assert get_dead_letters(db, REPO) == []
    assert lockfile.exists()
    lockfile.unlink()


def test_search_connection_input_respects_import_lockfile(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        126,
        title="locked connection search",
        files=[{"path": "conn.py", "patch_snippet": "LockedConnectionSearch"}],
        head_sha="conn",
        base_sha="b",
    )
    refresh_pr(db, REPO, 126)
    lockfile = pathlib.Path(f"{db}.import.lock")
    lockfile.write_text("locked", encoding="utf-8")
    conn = sqlite3.connect(db)
    try:
        hit = search_pr_overlap(
            conn,
            REPO,
            files=["conn.py"],
            error_messages=["LockedConnectionSearch"],
            require_fresh=True,
        )["results"][0]
    finally:
        conn.close()
        lockfile.unlink()

    assert hit["refresh_status"] == "import_lock_held"
    assert hit["archive_allowed"] is False
    assert health_snapshot(db)["replay_capsule_count"] == 0


def test_indexer_import_lockfile_returns_without_creating_db(tmp_path):
    db = tmp_path / "missing-live.db"
    pathlib.Path(f"{db}.import.lock").write_text("locked", encoding="utf-8")

    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 102,
                "title": "blocked without db",
                "head_sha": "blocked",
                "base_sha": "b",
                "files": [],
            }
        ]),
        min_free_disk_bytes=0,
    )

    assert summary["stop_reason"] == "import_lock_held"
    assert summary["run_id"] is None
    assert not db.exists()


def test_indexer_rejects_active_import_lock(tmp_path):
    db = tmp_path / "idx.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO indexer_locks(name,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?)",
        ("import:activate", "importer", time.time(), time.time()),
    )
    conn.commit()
    conn.close()

    summary = run_one_shot_indexer(
        db,
        REPO,
        client=FakePRClient([
            {
                "number": 98,
                "title": "blocked",
                "head_sha": "h",
                "base_sha": "b",
                "files": [],
            }
        ]),
        min_free_disk_bytes=0,
    )

    assert summary["capped"] is True
    assert summary["stop_reason"] == "lock_held"
    assert summary["run_id"]
    assert health_snapshot(db)["indexed_pr_count"] == 0


def test_smoke_metrics_report_health_coverage_and_query_timing(tmp_path):
    db = tmp_path / "idx.db"
    upsert_pr_revision(
        db,
        REPO,
        127,
        title="context compressor tool args",
        files=[
            {
                "path": "agent/context_compressor.py",
                "patch_snippet": "_summarize_tool_result handles string args",
            }
        ],
        head_sha="smoke",
        base_sha="base",
        index_scope="hot_open",
    )

    metrics = collect_smoke_metrics(
        db=db,
        repo=REPO,
        issue_number=59291,
        title="context compressor tool args",
        files=["agent/context_compressor.py"],
        symbols=["_summarize_tool_result"],
        runs=2,
    )

    assert metrics["ok"] is True
    assert metrics["health"]["schema_version"] == 3
    assert metrics["health"]["brownout_state"] in {"ok", "disk_brownout", "unknown"}
    assert metrics["term_coverage"]["latest_manifest_count"] == 1
    assert metrics["term_coverage"]["term_indexed_manifest_count"] == 1
    assert metrics["term_coverage"]["term_index_coverage_ratio"] == 1.0
    assert metrics["query"]["ran"] is True
    assert len(metrics["query"]["elapsed_ms_runs"]) == 2
    assert metrics["query"]["elapsed_ms_p50"] is not None
    assert metrics["query"]["candidate_prefilter"]["reason"] == "term_match"
    assert metrics["query"]["top_results"][0]["pr_number"] == 127
    assert "smoke_max_rss_bytes" in metrics["system"]
