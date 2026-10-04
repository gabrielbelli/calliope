"""One audit row as one stdout line: `audit {...}`.

The gateway keeps the audit table and also writes every row to stdout in this
form; a backend writes only these lines, because it has no table (§2.1). One
formatter for both means a log search for `audit {` finds every security event
in the estate in one shape, with the same column names as the table.

The row is JSON with `ensure_ascii`, so a username or a path carrying CR, LF
or any other control character cannot start a forged line of its own. Values,
bodies and tokens never go in a row; the caller is responsible for that, and
the fields below have no slot for one.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

__all__ = ["FIELDS", "ACTOR_KINDS", "OUTCOMES", "timestamp", "line", "emit"]

# The audit table's columns, in its order (§2.1), id excepted.
FIELDS = ("ts", "actor_kind", "actor_id", "auth_method", "ip", "action",
          "target", "outcome", "aggregated", "request_id", "detail")
ACTOR_KINDS = frozenset({"user", "api_key", "service", "cli", "anonymous"})
OUTCOMES = frozenset({"ok", "denied", "failed"})


def timestamp(seconds: float) -> str:
    """UTC ISO 8601 to the second, the form the audit table stores."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(row: Mapping[str, Any]) -> str:
    """The stdout form of one row. Raises ValueError on a row the table would refuse."""
    unknown = set(row) - set(FIELDS)
    if unknown:
        raise ValueError(f"not audit columns: {sorted(unknown)}")
    if row.get("actor_kind") not in ACTOR_KINDS:
        raise ValueError(f"actor_kind must be one of {sorted(ACTOR_KINDS)}")
    if row.get("outcome") not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    if not row.get("ts") or not row.get("action"):
        raise ValueError("ts and action are required")
    full = {field: row.get(field) for field in FIELDS}
    full["aggregated"] = 1 if full["aggregated"] else 0
    return "audit " + json.dumps(full, separators=(",", ":"), ensure_ascii=True)


def emit(row: Mapping[str, Any]) -> None:
    """Write one row to stdout, flushed, so it is never held in a buffer at a crash."""
    print(line(row), file=sys.stdout, flush=True)
