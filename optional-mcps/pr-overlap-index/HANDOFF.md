# PR Overlap Index MCP Handoff

## Branch

- Local branch: `codex/pr-overlap-index-mcp`
- Push target: `origin/codex/pr-overlap-index-mcp`
- Primary scope: optional MCP package at `optional-mcps/pr-overlap-index/`

## What This Branch Adds

- Local SQLite-backed PR overlap MCP server for Hermes issue triage.
- Latest-live PR revision manifests, superseded historical manifests, hunk hashes,
  term index, refresh leases, dead letters, and replay capsules.
- Read-only MCP query path: search, evidence lookup, health, and compatibility
  refresh suggestions.
- Bounded stdlib-only GitHub indexer for admin/systemd timer use.
- Offline open-PR backfill and safe import-bundle helpers.
- Read-only smoke metrics for VPS/operator checks.

## Current Safety Model

- MCP search does not call GitHub.
- Archive-class answers require latest-live evidence plus a fresh matching lease
  plus a replay capsule.
- Superseded manifests can only surface as `historical_overlap_only`.
- `cold_archive` is not used as direct archive authority in V1.
- Import activation validates the bundle, checks disk headroom, backs up the
  live DB family, takes an import lock, and can restore the rollback DB.

## Important Operational Boundary

Do not run long backfills in `/tmp`. macOS can remove `/tmp` contents between
sessions/reboots, which loses the builder DB and bundle. Use a persistent path:

```bash
BUILDER="$HOME/.hermes/data/pr-overlap-builder"
mkdir -p "$BUILDER/bundle"
```

Then run:

```bash
cd /path/to/hermes-agent

PYTHONPATH=optional-mcps/pr-overlap-index \
GITHUB_TOKEN="$GITHUB_TOKEN" \
python3 -m pr_overlap_index.offline_backfill backfill-open \
  NousResearch/hermes-agent \
  --db "$BUILDER/pr-overlap.db" \
  --bundle-dir "$BUILDER/bundle" \
  --max-elapsed-seconds 72000 \
  --batch-limit 100 \
  --max-requests 300 \
  --max-runtime-seconds 300 \
  --max-rss-mb 1200 \
  --min-free-disk-gb 5 \
  --max-files-per-pr 20 \
  --max-patch-bytes 2048
```

The command can run on another computer. The deliverable is the bundle
directory containing:

- `manifest.json`
- `pr-overlap-import.db`

Do not have two machines write the same SQLite DB. Multiple machines can build
separate bundles, but V1 does not merge bundles automatically; choose one
validated bundle to activate.

## Resilience Behavior

- GitHub TLS EOF, incomplete reads, remote disconnects, and 502/503/504 errors
  are treated as transient and retried.
- `offline_backfill` supervises repeated indexer passes and reconnects with
  exponential backoff.
- `KeyboardInterrupt` exits cleanly and exports the current bundle when
  `--bundle-dir` is set.
- Per-PR `/files` 404 is degraded to metadata-only ingestion so one odd PR does
  not stop the full backfill.
- PRs are checkpointed only after durable upsert; reprocessing a PR is allowed,
  skipping an uncommitted PR is not.

## Builder Validation

```bash
PYTHONPATH=optional-mcps/pr-overlap-index \
python3 -m pr_overlap_index.smoke \
  --db "$BUILDER/pr-overlap.db" --runs 5 --top-k 5

PYTHONPATH=optional-mcps/pr-overlap-index \
python3 -m pr_overlap_index.offline_backfill validate-bundle \
  --bundle-dir "$BUILDER/bundle" \
  --expected-repo NousResearch/hermes-agent
```

Useful fields:

- `health.brownout_state`
- `health.indexed_pr_count`
- `term_coverage.term_index_coverage_ratio`
- `query.elapsed_ms_p50` and `query.elapsed_ms_p95`
- `query.candidate_prefilter.reason`
- `health.last_run_by_scope.hot_open.stop_reason`
- `recent_passes[*].file_fetch_errors`

## VPS Activation Sketch

Stage the bundle under a data directory, not the service directory. Preserve
`run-indexer-safe.sh`; do not use `rsync --delete` against the service dir.

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

After activation:

```bash
python3 -m pr_overlap_index.smoke \
  --db /home/ubuntu/.hermes/data/pr-overlap-index/pr-overlap.db \
  --runs 5 --top-k 5
```

Keep the rollback DB path from activation until smoke metrics and real triage
queries look healthy.

## Verification Run

Latest local verification before handoff:

```bash
.venv/bin/python -m pytest -q tests/optional_mcps/test_pr_overlap_index.py
```

Expected result at handoff time:

```text
59 passed
```

## Known Follow-ups

- Keep long-running builder examples on persistent paths, not `/tmp`.
- V1 does not index comments/reviews/CI state. It is optimized for title/body,
  file path, bounded patch snippet, hashes, and extracted terms.
- V1 does not merge independent offline bundles. Treat a bundle activation as a
  replacement of the live DB after validation and rollback capture.
- Full cold archive serving should remain disabled until realistic cold-scale
  query tests prove local latency and memory are acceptable.
