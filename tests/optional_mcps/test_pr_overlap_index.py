import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = ROOT / "optional-mcps" / "pr-overlap-index"
sys.path.insert(0, str(PKG))

from pr_overlap_index import (  # noqa: E402
    IndexNotInitializedError,
    PrOverlapIndex,
    gc_replay_capsules,
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
from pr_overlap_index.indexer import (  # noqa: E402
    GitHubRestClient,
    RequestBudgetExceeded,
    run_one_shot_indexer,
)


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
    assert hit["source_updated_at"]
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
    assert snapshot["schema_version"] == 2
    assert snapshot["index_version"]["score"] == "lexical-v1"
    assert snapshot["indexed_pr_count"] == 1
    assert snapshot["latest_live_manifest_count"] == 1
    assert snapshot["tombstone_count"] == 1
    assert snapshot["active_lease_count"] == 1
    assert snapshot["dead_letter_count"] == 1
    assert snapshot["last_sync_at"]
    assert snapshot["last_refreshed_at"]


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
                "url": "https://api.github.example/repos/o/r/pulls/70",
                "html_url": "https://github.example/o/r/pull/70",
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
