# PR Overlap Index

Compact stdlib-only SQLite index for local PR-overlap evidence.

This optional MCP catalog package keeps the first implementation deliberately
small: the Python module stores PR revision manifests, latest-live evidence,
refresh leases, dead-lettered refresh failures, and replay capsules.  It does
not call GitHub and does not require the `mcp` package to import.

## MCP stdio smoke check

`server.py` is a minimal no-dependency JSON-RPC MCP stdio server. It reads newline-delimited JSON messages from stdin, writes JSON lines to stdout, and stays alive until stdin closes. It supports `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call` for `search_pr_overlap`, `get_pr_evidence`, `refresh_pr`, and `health`.

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' | python3 optional-mcps/pr-overlap-index/server.py
```

Set `PR_OVERLAP_DB` to choose the SQLite database. Without it, the server uses `./pr-overlap.db` under `PR_OVERLAP_INDEX_ROOT` or the package directory. Programmatic callers use
`pr_overlap_index.core` functions such as `init_db`, `upsert_pr_revision`,
`refresh_pr`, `search_pr_overlap`, `get_pr_evidence`, `record_refresh_failure`,
`get_dead_letters`, and `replay_capsule`.

Archive-class overlap responses are intentionally fail-closed: a `full-cover`
answer needs latest-live evidence, a live matching refresh lease, and a
materialized replay capsule.  Superseded evidence can be returned only as
`historical_overlap_only`.

When `search_pr_overlap(require_fresh=True)` finds a full-cover candidate without a live lease, callers can surface refresh state without network access: `refresh_budget_available=False` returns `refresh_status: suggestion_only`, while `refresh_failure_reason=...` records a dead-letter row and returns `refresh_status: dead_lettered`.
