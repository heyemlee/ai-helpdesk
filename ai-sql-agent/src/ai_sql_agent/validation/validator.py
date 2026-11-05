from __future__ import annotations

from typing import Dict, Optional

from .cardinality import check_cardinality
from .shape import (
    check_shape_against_plan,
    check_unrequested_aggregation,
)
from .objects import (
    SELF_CHECK_NOT_APPLICABLE,
    SELF_CHECK_NOT_RUN,
    AnomalyType,
    ValidationFinding,
    ValidationOutcome,
)
from .self_check import SemanticSelfChecker


class ResultValidator:
    """
    执行结果校验器：处理「SQL 跑通了但结果可能是错的」这一类软异常。

    硬错误（语法、未知字段）由 ai_sql_agent.repair 的日志解析回路负责，不在这里。

    设计原则：`执行成功` ≠ `答案正确`。只校验执行是否成功会漏掉最危险的一类
    错误——join 扇出导致聚合重复计数、过滤条件写反导致空结果，这些 SQL 都能
    正常返回，只有结果是错的。
    """

    def __init__(
        self,
        self_checker: Optional[SemanticSelfChecker] = None,
        *,
        enable_cardinality: bool = True,
        enable_self_check: bool = True,
    ):
        self.self_checker = self_checker or SemanticSelfChecker()
        self.enable_cardinality = enable_cardinality
        self.enable_self_check = enable_self_check

    def validate(
        self,
        *,
        query: str,
        sql: str,
        row_count: int,
        expected_cardinality: str = "",
        is_final_step: bool = True,
        preceding_summary: str = "",
        step_objective: str = "",
        column_units: Optional[Dict[str, str]] = None,
    ) -> ValidationOutcome:
        findings: list[ValidationFinding] = []
        paraphrase: Optional[str] = None

        # 1. 空结果
        if row_count == 0:
            findings.append(
                ValidationFinding(
                    anomaly=AnomalyType.EMPTY_RESULT,
                    detail="查询返回 0 行。可能确实没有符合条件的数据，也可能是过滤条件写错。",
                    severity="warn",
                )
            )

        # 2. 基数校验
        if self.enable_cardinality and expected_cardinality:
            finding = check_cardinality(expected_cardinality, row_count, sql=sql)
            if finding is not None:
                findings.append(finding)

        # 3. 形状检查：静态、不调模型，因此对每条查询都跑。
        #    实测静默错误的头号形态就是形状不符（该出明细却聚合成一个数）。
        if is_final_step:
            finding = check_unrequested_aggregation(query, sql)
            if finding is not None:
                findings.append(finding)
        finding = check_shape_against_plan(expected_cardinality, sql)
        if finding is not None:
            findings.append(finding)

        # 4. 语义自检。中间步骤拿**该步骤的目标**去比，不能拿用户原问题——
        #    第一步只做前半段，拿整个问题去比必然判成「漏掉了后半段」，
        #    那正是之前跨库误报 100% 的成因。
        target = query if is_final_step else step_objective
        self_check = SELF_CHECK_NOT_APPLICABLE
        self_check_error = ""
        if (
            self.enable_self_check
            and self.self_checker.available
            and row_count > 0
            and target.strip()
        ):
            result = self.self_checker.run(
                target,
                sql,
                preceding_summary=preceding_summary if is_final_step else "",
                known_units=column_units or None,
            )
            self_check, self_check_error = result.status, result.error
            paraphrase = result.paraphrase
            if result.finding is not None:
                findings.append(result.finding)
            if self_check == SELF_CHECK_NOT_RUN:
                # 该做的核对没做成：留一条发现，让它和「核对过、没问题」在响应里长得不一样。
                # warn 而不是 block——结果未必是错的，没有理由为此回溯重跑。
                cause = (
                    "模型的回答里读不出结论"
                    if self_check_error == "UnreadableVerdict"
                    else f"模型调用失败（{self_check_error or '未知错误'}）"
                )
                findings.append(
                    ValidationFinding(
                        anomaly=AnomalyType.SELF_CHECK_NOT_RUN,
                        detail=(
                            f"语义自检未执行：{cause}。"
                            "本步结果没有经过「SQL 实际行为是否回答了问题」这道核对。"
                        ),
                        severity="warn",
                    )
                )

        return ValidationOutcome(
            passed=(
                not any(f.severity == "block" for f in findings)
                and self_check != SELF_CHECK_NOT_RUN
            ),
            findings=findings,
            self_check_paraphrase=paraphrase,
            self_check=self_check,
            self_check_error=self_check_error,
        )
