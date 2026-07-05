#!/usr/bin/env python3
"""No-dependency JSON-RPC stdio server for the optional PR overlap index.

The optional MCP package must remain importable without the third-party `mcp`
package.  This file therefore implements the tiny MCP stdio surface used by
catalog smoke tests directly with newline-delimited JSON-RPC messages.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from pr_overlap_index import (
    get_pr_evidence,
    health_snapshot,
    init_db,
    refresh_pr,
    search_pr_overlap,
)

SERVER_NAME = "pr-overlap-index"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"
TOOLS = ("search_pr_overlap", "get_pr_evidence", "refresh_pr", "health")


def _db_path() -> Path:
    explicit = os.environ.get("PR_OVERLAP_DB")
    if explicit:
        return Path(explicit)
    root = Path(
        os.environ.get("PR_OVERLAP_INDEX_ROOT", Path(__file__).resolve().parent)
    )
    return root / "pr-overlap.db"


def health() -> dict[str, object]:
    db = _db_path()
    snapshot = health_snapshot(db)
    return {
        "name": SERVER_NAME,
        "mcp_dependency_required_for_import": False,
        "db_path": str(db),
        **snapshot,
    }


def _tool_descriptions() -> list[dict[str, Any]]:
    object_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}
    return [
        {
            "name": "search_pr_overlap",
            "description": "Search local PR overlap evidence.",
            "inputSchema": object_schema,
        },
        {
            "name": "get_pr_evidence",
            "description": "Return indexed evidence for one PR.",
            "inputSchema": object_schema,
        },
        {
            "name": "refresh_pr",
            "description": "Issue a local refresh lease or surface budget/DLQ state.",
            "inputSchema": object_schema,
        },
        {
            "name": "health",
            "description": "Return server health metadata.",
            "inputSchema": object_schema,
        },
    ]


def _content(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]}


def _call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = dict(arguments or {})
    db = _db_path()
    init_db(db)
    if name == "health":
        return _content(health())
    if name == "search_pr_overlap":
        repo = args.pop("repo")
        issue = args.pop("issue", None)
        if isinstance(issue, dict):
            for key, value in issue.items():
                args.setdefault(key, value)
        return _content(search_pr_overlap(db, repo, **args))
    if name == "get_pr_evidence":
        return _content(get_pr_evidence(db, args["repo"], int(args["pr_number"])))
    if name == "refresh_pr":
        return _content(
            refresh_pr(db, args.pop("repo"), int(args.pop("pr_number")), **args)
        )
    raise KeyError(f"Unknown tool: {name}")


def _result(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {}
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {}},
        }
    if method == "tools/list":
        return {"tools": _tool_descriptions()}
    if method == "tools/call":
        params = params or {}
        return _call_tool(str(params.get("name", "")), params.get("arguments") or {})
    raise NotImplementedError(method)


def _error_response(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = str(message.get("method", ""))
    try:
        result = _result(method, message.get("params") or {})
    except NotImplementedError:
        return _error_response(message_id, -32601, f"Method not found: {method}")
    except (
        Exception
    ) as exc:  # JSON-RPC server errors should be data, not stderr tracebacks.
        return _error_response(message_id, -32000, str(exc))
    if message_id is None or result is None:
        return None
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error_response(None, -32700, f"Parse error: {exc}")
        else:
            response = _handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
