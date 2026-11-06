"""Evidence cells are derived exclusively from execution rows, never answer prose."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class BusinessEvidence(BaseModel):
    evidence_id: str
    entity: dict[str, Any]
    field: str
    value: Any
    unit: str | None = None
    as_of: str | None = None
    as_of_source: str | None = None
    database: str
    step_index: int
    row_index: int
    query_hash: str
    source_records: list[dict[str, Any]] = []
    issues: list[str] = []


def cells(steps) -> list[BusinessEvidence]:
    out = []
    for si, step in enumerate(steps):
        if not step.success or step.error or step.risk_findings or step.row_count != len(step.rows):
            continue
        query_hash = hashlib.sha256(step.final_sql.encode()).hexdigest()
        try:
            dated = bool(datetime.fromisoformat((step.as_of or "").replace("Z", "+00:00")).tzinfo)
        except ValueError:
            dated = False
        for ri, row in enumerate(step.rows):
            # A result-row identity preserves joint witnesses, even for aggregates.
            # It is not a guessed business primary key or a manufactured order id.
            entity = {"kind": "query_row", "query_hash": query_hash, "row_index": ri}
            for name, value in row.items():
                if value is None or isinstance(value, (list, dict)):
                    continue
                issues = []
                if isinstance(value, (int, float)) and not isinstance(value, bool) and not step.column_units.get(name):
                    issues.append("missing_unit")
                if not dated or step.as_of_source not in {"query_time", "snapshot"}:
                    issues.append("missing_time")
                if not step.final_sql or not step.database:
                    issues.append("missing_provenance")
                if step.truncated:
                    issues.append("truncated")
                identity = [step.database, query_hash, step.as_of, si, ri, name, value]
                eid = "ev_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
                out.append(BusinessEvidence(
                    evidence_id=eid, entity=entity, field=name, value=value,
                    unit=step.column_units.get(name), as_of=step.as_of, as_of_source=step.as_of_source,
                    database=step.database, step_index=si, row_index=ri, query_hash=query_hash,
                    source_records=step.source_records, issues=issues,
                ))
    return out
