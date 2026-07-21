# PR Overlap Index

Compact stdlib-only SQLite index for local PR-overlap evidence.

This optional MCP catalog package keeps the first implementation deliberately
small: the Python module stores PR revision manifests, latest-live evidence,
refresh leases, dead-lettered refresh failures, and replay capsules.  MCP query
tools do not call GitHub and do not require the `mcp` package to import.

## MCP stdio smoke check

`server.py` is a minimal no-dependency JSON-RPC MCP stdio server. It reads newline-delimited JSON messages from stdin, writes JSON lines to stdout, and stays alive until stdin closes. It supports `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call` for `search_pr_overlap`, `get_pr_evidence`, `refresh_pr`, and `health`.

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' | python3 optional-mcps/pr-overlap-index/server.py
```

## Operator smoke metrics

`pr_overlap_index.smoke` emits a single read-only JSON object for operator
checks. It does not call GitHub and does not write the index. The output
includes health, term-index coverage, query timing, candidate prefilter status,
compact top hits, process RSS, load average, and Linux memory counters when
available.

```bash
PYTHONPATH=/home/ubuntu/.hermes/services/pr-overlap-index \
PR_OVERLAP_DB=/home/ubuntu/.hermes/data/pr-overlap-index/pr-overlap.db \
python3 -m pr_overlap_index.smoke --runs 5 --top-k 5
```

Useful fields to watch:

- `health.schema_version` and `health.brownout_state`
- `term_coverage.term_indexed_manifest_count` /
  `term_coverage.latest_manifest_count`
- `query.elapsed_ms_p50`, `query.elapsed_ms_p95`, and
  `query.candidate_prefilter.reason`
- `health.last_run_by_scope.*.stop_reason`, `requests_used`, and `indexed`
- `system.memavailable_bytes`, `system.swapfree_bytes`, and
  `health.free_disk_bytes`

Set `PR_OVERLAP_DB` to choose the SQLite database. Without it, the server uses `./pr-overlap.db` under `PR_OVERLAP_INDEX_ROOT` or the package directory. Health and query tools are fail-closed: they report an uninitialized index instead of creating a database implicitly. Programmatic callers use
`pr_overlap_index.core` functions such as `init_db`, `upsert_pr_revision`,
`refresh_pr`, `search_pr_overlap`, `get_pr_evidence`, `record_refresh_failure`,
`get_dead_letters`, and `replay_capsule`.

Health includes coverage provenance for indexed scopes when available:
`coverage_by_scope`, `last_run_by_scope`, `db_bytes`, `wal_bytes`,
`shm_bytes`, `free_disk_bytes`, `brownout_state`, and
`archive_confidence_floor`.  Coverage denominators are explicit: an unknown
denominator is reported as `coverage_denominator_kind: "unknown"` rather than
computed with live network I/O from the MCP health path.

Archive-class overlap responses are intentionally fail-closed: a `full-cover`
answer needs latest-live evidence, a live matching refresh lease, and a
materialized replay capsule.  Superseded evidence can be returned only as
`historical_overlap_only`.

`cold_archive` evidence is not part of the default serving set. Search keeps
legacy/manual entries and hot/recent scoped PRs visible, but excludes PRs whose
only known scope is `cold_archive` until an explicit cold-scale serving gate is
implemented.

When `search_pr_overlap(require_fresh=True)` finds a full-cover candidate without a live lease, it remains local-only and returns `needs-refresh`. Callers can surface refresh state without network access: `refresh_budget_available=False` returns `refresh_status: suggestion_only`, while `refresh_failure_reason=...` records a dead-letter row and returns `refresh_status: dead_lettered`.

The public MCP `refresh_pr` compatibility tool cannot mint archive-eligible
leases. Without `PR_OVERLAP_ENABLE_ADMIN_MUTATION=1`, it returns
`refresh_status: "suggestion_only"` for initialized indexes and rejects revision
mutation fields such as `title`, `body`, `files`, `head_sha`, and `base_sha`.
Only an explicit admin/indexer process can create fresh leases or mutate
revision evidence.

## One-shot indexer

`pr_overlap_index.indexer` provides a bounded stdlib-only one-shot indexer for
systemd timer or manual admin runs. It is not used by MCP query tools.

```bash
python3 -m pr_overlap_index.indexer NousResearch/hermes-agent \
  --db /var/lib/pr-overlap/pr-overlap.db \
  --scope open \
  --max-requests 80 \
  --max-runtime-seconds 45 \
  --max-rss-mb 250 \
  --min-free-disk-gb 4
```

The indexer stores metadata, changed paths, hashes, terms, and bounded patch
snippets first. Full patch/comment/review payloads should be hydrated only for
high-value candidates or explicit admin refreshes.

The default one-shot caps are intentionally VPS-safe for the small 2C/2GB
serving box: 45 seconds wall time, 250MB RSS, and 4GB free-disk brownout.
Raise them only on a larger offline builder or a temporary worker.

Incremental runs persist per-repo/per-scope JSON watermarks in
`indexer_watermarks`. A capped run keeps a bounded resume hint containing
already-processed PR revision keys, so the next run can move past the same
newest PRs instead of re-fetching their file details. A complete run advances
the upstream `updated_at` watermark and clears the temporary skip set.
Indexer runs refuse to start while an import lockfile exists. The lockfile is
deliberately conservative: age alone does not make it safe to delete or bypass.

Legacy `--scope` values remain supported for cron compatibility:

- `open` maps to `hot_open`
- `recent` maps to `recent_closed`
- `closed` maps to bounded `recent_closed` behavior, not unbounded cold archive

The indexer stores GitHub's upstream `updated_at` as `source_updated_at`.
Local capture/run time is tracked separately in indexer run metadata so a
re-ingest does not make old upstream evidence appear newer than it is.

Indexer runs are recorded in the local DB with profile, scope, caps, stop
reason, request count, DB/WAL bytes, and free disk bytes.  This lets operators
tell the difference between "no overlap found" and "the hot index has not
covered that part of PR history yet."

## Cold import safety helpers

The V1 serving path does not run broad cold-history ingestion on a small VPS by
default.  Cold archive artifacts should be built off-VPS or on a larger
temporary worker, then staged and validated before serving.

## Offline open-PR backfill workflow

Use `pr_overlap_index.offline_backfill` to build a larger open-PR index away
from the small serving VPS, then import the resulting bundle explicitly. The
module is stdlib-only and deliberately does not run SSH, rsync, or service
restarts by itself.

Recommended operator flow:

1. Pull a snapshot of the live VPS DB to the offline builder. Use the snapshot
   as the starting DB so the later activation does not discard recent hot
   evidence already present on the VPS.
2. Run a bounded high-cap backfill locally:

   ```bash
   BUILDER="$HOME/.hermes/data/pr-overlap-builder"
   mkdir -p "$BUILDER/bundle"

   PYTHONPATH=optional-mcps/pr-overlap-index \
   GITHUB_TOKEN=<token> \
   python3 -m pr_overlap_index.offline_backfill backfill-open \
     NousResearch/hermes-agent \
     --db "$BUILDER/pr-overlap.db" \
     --bundle-dir "$BUILDER/bundle" \
     --batch-limit 300 \
     --max-requests 800 \
     --max-runtime-seconds 900 \
     --max-rss-mb 1500 \
     --min-free-disk-gb 10 \
     --max-files-per-pr 20 \
     --max-patch-bytes 2048
   ```

   The command loops one-shot indexer passes until GitHub pagination completes
   or an explicit cap is reached. It prints JSON with `indexed_total`,
   `requests_used_total`, before/after health, recent pass summaries, and bundle
   export status.
3. Validate and smoke the builder DB before upload:

   ```bash
   PYTHONPATH=optional-mcps/pr-overlap-index \
   python3 -m pr_overlap_index.smoke \
     --db "$BUILDER/pr-overlap.db" --runs 5 --top-k 5

   PYTHONPATH=optional-mcps/pr-overlap-index \
   python3 -m pr_overlap_index.offline_backfill validate-bundle \
     --bundle-dir "$BUILDER/bundle" \
     --expected-repo NousResearch/hermes-agent
   ```

4. Upload the bundle directory to a staging path on the VPS. Preserve the
   existing `run-indexer-safe.sh`; do not rsync with `--delete` against the
   service directory.
5. On the VPS, run preflight and activation explicitly:

   ```bash
   export PYTHONPATH=/home/ubuntu/.hermes/services/pr-overlap-index

   python3 -m pr_overlap_index.offline_backfill preflight \
     --live-db /home/ubuntu/.hermes/data/pr-overlap-index/pr-overlap.db \
     --staging-db /home/ubuntu/.hermes/data/pr-overlap-index/staging/bundle/pr-overlap-import.db \
     --min-free-disk-gb 4

   python3 -m pr_overlap_index.offline_backfill activate-bundle \
     --live-db /home/ubuntu/.hermes/data/pr-overlap-index/pr-overlap.db \
     --bundle-dir /home/ubuntu/.hermes/data/pr-overlap-index/staging/bundle \
     --expected-repo NousResearch/hermes-agent \
     --rollback-dir /home/ubuntu/.hermes/data/pr-overlap-index/rollback \
     --min-free-disk-gb 4
   ```

6. Run the smoke command on the VPS and keep the reported rollback DB path until
   p50/p95, term coverage, `brownout_state`, free disk, and normal triage
   queries look healthy.

If activation fails, the live DB is left unchanged. If post-activation smoke
fails, restore the rollback DB with:

```bash
python3 -m pr_overlap_index.offline_backfill restore-backup \
  --live-db /home/ubuntu/.hermes/data/pr-overlap-index/pr-overlap.db \
  --rollback-db <rollback_db_from_activate_output>
```

Programmatic helpers:

- `export_cold_import_bundle(source_db, bundle_dir)` creates a validated
  SQLite artifact plus a manifest containing schema version, index version,
  repo identity, and row counts.
- `validate_cold_import_bundle(bundle_dir, expected_repo=...)` verifies the
  manifest against an in-bundle artifact path and runs SQLite integrity/schema
  validation.
- `activate_cold_import_bundle(live_db, bundle_dir, ...)` validates the bundle,
  checks disk headroom, backs up the live DB family, validates an incoming copy,
  and atomically replaces the live DB path while holding an import lock that
  rejects concurrent indexer/import actions.
- `restore_import_backup(live_db, rollback_db)` restores the rollback DB
  created during activation while holding the same conservative import lock and
  rejecting active DB-level indexer/import locks.
- `import_disk_preflight(live_db, staging_db)` checks that the filesystem can
  hold the live DB family, staging DB family, rollback DB family, WAL/SHM
  sidecars, and the configured brownout reserve before activation.
- `validate_staging_import_db(staging_db)` runs a mandatory WAL checkpoint,
  SQLite integrity check, and schema-version validation on a staging DB.

Broad cold archive serving should stay disabled until cold-scale query tests
prove that the term/path prefilter keeps local MCP queries within the latency
target on a realistic repository-sized fixture. Until that gate passes,
`cold_archive` coverage is reported as offline/investigation-only and does not
raise `archive_confidence_floor` to `hot_plus_cold`.
