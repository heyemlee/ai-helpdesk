"""
把 pipeline 的结果归纳成响应 / 审计 / A2A artifact 共用的一份摘要。

以前这段逻辑只在 /query 里，A2A 来了之后 message/send、message/stream 都要同一份
——契约 §4.4 要求 artifact 的 data part 与 /query 的返回**完全同构**，「调用方走 send
还是走 stream，能做的信任判断必须一模一样」。三处各写一遍必然漂移，所以抽出来。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from ai_sql_agent.api.business_evidence import BusinessEvidence, cells
from ai_sql_agent.audit import AuditRecord
from ai_sql_agent.core import log_event, usage_summary
from ai_sql_agent.core.logging_setup import get_logger

_logger = get_logger("api")


# 契约 §4.6.1：业务终态的闭集。
#
# §4.4 的 `state` 是**传输**终态，只回答「载荷有没有完整送达」。查询全挂时
# pipeline 不抛异常，`state` 照样是 completed、`queried_database` 照样是 true——
# 调用方于是没有任何字段能发现这次查询挂了，然后把模型编的话当成系统数据讲给客户。
# 这一层就是补那个洞。
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_SUCCESS = "success"
STATUS_NO_DATA = "no_data"
STATUS_PARTIAL_SUCCESS = "partial_success"
STATUS_FAILED = "failed"

DATABASE_QUERY_ROUTE = "database_query"


class StepLogResponse(BaseModel):
    database: str
    sql: str
    final_sql: str
    success: bool
    row_count: int
    truncated: bool = False
    rows: List[Dict[str, Any]] = []
    error: Optional[str] = None
    repair_attempts: List[Dict[str, Any]] = []
    risk_findings: List[Dict[str, Any]] = []
    """
    软异常校验发现（空结果 / 基数异常 / 语义不一致）。

    必须回传给调用方：这些错误 SQL 跑得通、有结果，不提示的话用户看不出异常，
    会把错的数字当成事实。指标口径上，「答错且没有任何提示」才算静默错误——
    如果提示只写进服务端日志，那对用户来说它依然是静默的。
    """

    row_limit: Optional[int] = None
    """
    这一步生效的行数上限（契约 §4.6.3）。`truncated=true` 时必须有值——
    否则调用方分不清「服务配置的上限封顶」还是「被临时调小的上限截了」。
    """

    observed_at: Optional[str] = None
    as_of: Optional[str] = None
    """数据时点，RFC 3339（契约 §4.6.4）。"""

    as_of_source: Optional[str] = None
    """`query_time` / `snapshot` / `unknown`（契约 §4.6.4）。"""

    column_units: Dict[str, str] = {}
    """结果列 → 单位标识（契约 §4.6.5）。判不出来的列不出现。"""

    column_metadata: Dict[str, Dict[str, str]] = {}
    """Optional configured field meaning, bound to the executed SQL and source."""

    source_records: List[Dict[str, Any]] = []
    """来源记录标识（契约 §4.6.6）。给不出来时为空，溯源退回 trace_id + final_sql。"""


@dataclass
class ResultSummary:
    route: str
    confidence: float
    reason: str
    answer: str
    queried_database: bool
    step_logs: List[StepLogResponse] = field(default_factory=list)
    selected_databases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    sqls: List[Dict[str, str]] = field(default_factory=list)
    audit_sqls: List[Dict[str, str]] = field(default_factory=list)
    """
    写进审计的步骤列表，模型不可用时按它重放。和 `sqls` 的区别：没生成 SQL 就答了「没有」
    的步骤（依赖的步骤 0 行，见 `Text2SQLPipeline._dependent_step_outcome`）按原位置留一项、
    sql 为空。少了它，审计里就是一份砍掉后半截的计划，重放时只跑前几步、当成完整答案；
    留着空项，`audit.store._parse_steps` 会整条放弃重放。
    """
    denied_tables: List[str] = field(default_factory=list)
    failed_steps: int = 0
    repaired_steps: int = 0
    has_risk: bool = False
    degraded: bool = False
    degrade_reason: str = ""

    # ---- 契约 §4.6 业务终态语义 ----
    execution_status: str = STATUS_FAILED
    """§4.6.1。默认 failed：算不出来就是最不可信的那一档，不是最可信的。"""

    succeeded_steps: int = 0
    """§4.6.2。与 failed_steps 相加必须等于 len(steps)。"""

    result_truncated: bool = False
    """§4.6.3 = any(steps[].truncated)，让调用方一跳拿到。"""

    as_of: Optional[str] = None
    """§4.6.4。跨库时取**最早**的那个时点，见 summarize_result。"""

    as_of_source: Optional[str] = None
    currency: Optional[str] = None
    """§4.6.5。多币种时为 None，调用方逐列读 steps[].column_units。"""

    evidence: List[BusinessEvidence] = field(default_factory=list)
    observed_at: Optional[str] = None

    def data_part(self) -> Dict[str, Any]:
        """
        artifact 的 data part（契约 §4.4 / §4.6）。

        §4.4 要求 `/query`、A2A `message/send`、A2A `message/stream` 三者
        **完全同构**——「调用方走 send 还是走 stream，能做的信任判断必须一模一样」。
        三者都先用 `summarize_result()` 把 pipeline 结果归纳成 `ResultSummary`，
        但调这个函数的只有 A2A：send 与 stream 都经
        `ai_sql_agent.a2a.server._artifact()` 走到这里。这两条各写一遍必然漂，
        所以只有这一份。

        `/query` 不调它：`ai_sql_agent.api.app` 的 `query()` 逐字段填
        `QueryResponse`。那一侧与这里的对齐靠 `tests/test_business_outcome.py`
        守着（响应体与 data part 过同一份契约 schema，并比对三个承载者的业务字段），
        不是靠共用这个函数——这里加字段，`QueryResponse` 与 `query()` 也要跟着加。
        """
        return {
            "route": self.route,
            "confidence": self.confidence,
            "queried_database": self.queried_database,
            "has_risk": self.has_risk,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "selected_databases": self.selected_databases,
            "execution_status": self.execution_status,
            "succeeded_steps": self.succeeded_steps,
            "failed_steps": self.failed_steps,
            "repaired_steps": self.repaired_steps,
            "result_truncated": self.result_truncated,
            "as_of": self.as_of,
            "as_of_source": self.as_of_source,
            "currency": self.currency,
            "observed_at": self.observed_at,
            "evidence": [item.model_dump() for item in self.evidence],
            "steps": [step.model_dump() for step in self.step_logs],
        }


def compute_execution_status(route: str, step_logs: List[StepLogResponse]) -> str:
    """
    契约 §4.6.1。判据全部由 `steps[]` 算出，不需要任何新的执行信息。

    判定顺序有讲究：`failed` 必须先判。空步骤列表下 `all([])` 是 True、
    `sum([]) == 0`，先判 success/no_data 会把「查库路径但一步都没有」
    错判成 no_data——而契约明确要求它是 failed。
    """
    if route != DATABASE_QUERY_ROUTE:
        return STATUS_NOT_APPLICABLE
    if not any(step.success for step in step_logs):
        return STATUS_FAILED
    if not all(step.success for step in step_logs):
        return STATUS_PARTIAL_SUCCESS
    total_rows = sum(step.row_count for step in step_logs)
    return STATUS_SUCCESS if total_rows > 0 else STATUS_NO_DATA


def parse_denied_tables(error: str) -> List[str]:
    """从越权错误信息里取回被拒的表名，落进审计。（与 /query 原实现逐字相同）"""
    marker = "没有访问权限："
    if marker not in error:
        return []
    tail = error.split(marker, 1)[1]
    tail = tail.split("。", 1)[0]
    return [item.strip() for item in tail.split(",") if item.strip()]


def summarize_result(result) -> ResultSummary:
    step_logs: List[StepLogResponse] = []
    selected: List[str] = []
    tables: List[str] = []
    audit_sqls: List[Dict[str, str]] = []
    if result.pipeline_result is not None:
        selected = list(result.pipeline_result.selected_databases)
        for log in result.pipeline_result.step_logs:
            execution = log.execution_result
            if log.final_sql or log.sql:
                audit_sqls.append({"database": log.database, "sql": log.final_sql or log.sql})
            elif execution.get("held_reason") and execution.get("success"):
                audit_sqls.append({"database": log.database, "sql": ""})
            truncated = bool(execution.get("truncated"))
            row_limit = execution.get("row_limit")
            step_logs.append(StepLogResponse(
                database=log.database, sql=log.sql, final_sql=log.final_sql or log.sql,
                success=bool(execution.get("success")),
                row_count=int(execution.get("row_count") or 0),
                truncated=truncated,
                rows=list(execution.get("rows") or []),
                error=execution.get("error"),
                repair_attempts=list(log.repair_attempts),
                risk_findings=list((log.validation or {}).get("findings") or []),
                # §4.6.3：truncated 为真时 row_limit 必须有值。执行器一定会给，
                # 这里兜的是「历史记录回放 / 旧格式」这类拿不到的情况。
                row_limit=(int(row_limit) if row_limit is not None else None),
                observed_at=(execution.get("observed_at") or None),
                as_of=(execution.get("as_of") or None),
                as_of_source=(execution.get("as_of_source") or None),
                column_units=dict(getattr(log, "column_units", {}) or {}),
                column_metadata=dict(getattr(log, "column_metadata", {}) or {}),
                source_records=list(getattr(log, "source_records", []) or []),
            ))
        tables = sorted({t for ts in result.pipeline_result.selected_tables.values() for t in ts})

    failed = sum(1 for s in step_logs if not s.success)
    succeeded = len(step_logs) - failed
    repaired = sum(1 for s in step_logs if s.repair_attempts)
    # 两条路各有各的降级：查库那条记在 pipeline_result 上，对话问答那条记在 result 上
    if result.pipeline_result is not None:
        degraded = bool(result.pipeline_result.degraded)
        degrade_reason = result.pipeline_result.degrade_reason
    else:
        degraded = bool(getattr(result, "degraded", False))
        degrade_reason = str(getattr(result, "degrade_reason", "") or "") if degraded else ""
    route = result.decision.route.value

    as_of, as_of_source = _resolve_as_of(step_logs)
    from ai_sql_agent.pipeline.provenance import dominant_currency

    return ResultSummary(
        route=route, confidence=result.decision.confidence,
        reason=result.decision.reason, answer=result.answer,
        queried_database=result.queried_database,
        evidence=cells(step_logs), observed_at=_observed_at(step_logs),
        step_logs=step_logs, selected_databases=selected, tables=tables,
        sqls=[{"database": s.database, "sql": s.final_sql} for s in step_logs if s.final_sql],
        audit_sqls=audit_sqls,
        denied_tables=sorted({t for s in step_logs if not s.success and s.error and "没有访问权限" in s.error
                              for t in parse_denied_tables(s.error)}),
        failed_steps=failed, repaired_steps=repaired,
        has_risk=any(s.risk_findings for s in step_logs),
        degraded=degraded,
        degrade_reason=degrade_reason,
        execution_status=compute_execution_status(route, step_logs),
        succeeded_steps=succeeded,
        result_truncated=any(s.truncated for s in step_logs),
        as_of=as_of,
        as_of_source=as_of_source,
        currency=dominant_currency([s.column_units for s in step_logs]),
    )


def _observed_at(steps):
    # Only original successful execution timestamps count. Re-summarizing an old
    # result/checkpoint must never turn it into a newly checked fact.
    try:
        times = [datetime.fromisoformat(s.observed_at.replace("Z", "+00:00")) for s in steps if s.success]
    except (ValueError, AttributeError):
        return None
    return min(times).isoformat() if times and all(t.tzinfo for t in times) else None


def _resolve_as_of(step_logs: List[StepLogResponse]):
    """
    顶层数据时点（契约 §4.6.4）。

    跨库查询两步的时点可能不同，顶层只有一个值，所以取**最早**的那个——
    「这份数据至少有多旧」是唯一安全的读法。各步自己的时点仍在 `steps[].as_of` 里。

    只看成功的步骤：失败那步没读到数据，它的时点不代表任何东西。
    来源不一致（一步读主库、一步读快照）时降级为 `unknown`，
    契约要求这种情况下调用方不得表述时点。
    """
    from datetime import datetime
    successful = [s for s in step_logs if s.success]
    if any(not s.as_of or s.as_of_source not in {"query_time", "snapshot"} for s in successful):
        return None, "unknown"
    try:
        stamps = [datetime.fromisoformat(s.as_of.replace("Z", "+00:00")) for s in successful]
        if any(not t.tzinfo for t in stamps):
            return None, "unknown"
    except ValueError:
        return None, "unknown"
    if not stamps:
        return None, None
    sources = {s.as_of_source for s in step_logs if s.success and s.as_of}
    source = sources.pop() if len(sources) == 1 else "unknown"
    return min(stamps).isoformat(), (source or "unknown")


def apply_summary_to_audit(summary: ResultSummary, audit: AuditRecord) -> None:
    audit.route = summary.route
    audit.databases = summary.selected_databases
    audit.tables = summary.tables
    audit.sqls = summary.audit_sqls
    audit.denied_tables = summary.denied_tables
    audit.success = summary.failed_steps == 0 and not summary.degraded
    audit.degraded = summary.degraded
    audit.has_risk = summary.has_risk
    audit.execution_status = summary.execution_status
    audit.failed_steps = summary.failed_steps
    audit.repaired_steps = summary.repaired_steps


def run_outcome(summary: "ResultSummary") -> Dict[str, Any]:
    """写进 run.finished 的业务结论，与审计同一份来源——两边各算一份就会各说各话。"""
    return {
        "route": summary.route,
        "degraded": summary.degraded,
        "degrade_reason": summary.degrade_reason,
        "execution_status": summary.execution_status,
        "steps": len(summary.step_logs),
        "failed_steps": summary.failed_steps,
        "repaired_steps": summary.repaired_steps,
        "has_risk": summary.has_risk,
    }


def write_audit(app, audit: AuditRecord, started: float, principal) -> None:
    """写审计记录；审计失败绝不影响查询本身。（与 /query 的 _write_audit 同一实现）"""
    usage = usage_summary()
    audit.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    audit.prompt_tokens = usage["prompt_tokens"]
    audit.completion_tokens = usage["completion_tokens"]
    try:
        app.state.audit.record(audit)
        app.state.limiter.quota_guard.invalidate(principal.user_id)
    except Exception as exc:
        log_event(_logger, logging.ERROR, "audit.write_failed",
                  error=f"{type(exc).__name__}: {exc}")
