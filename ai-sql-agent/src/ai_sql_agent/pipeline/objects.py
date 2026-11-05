from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .token_budget import PromptBudgetConfig


@dataclass
class DatabaseConfig:
    """单个数据库配置。"""

    name: str
    db_path: str | Path = ""
    """SQLite 库文件路径；dialect 为 postgres 时忽略，改用 dsn。"""

    dialect: str = "sqlite"
    """目标库方言（见 ai_sql_agent.execution.dialects）：决定生成 Prompt 的语法约束与执行器类型。"""

    dsn: str = ""
    """PostgreSQL 连接串；dialect="postgres" 时必填。"""


@dataclass
class PipelineConfig:
    """端到端流程配置。"""

    database_name: str = "erp_db"
    db_path: str | Path = ""
    """
    覆盖 `database_name` 那个库的连接路径，**留空表示不覆盖**。

    留空是有原因的。这两个字段是「换一个 SQLite 文件跑一下」的便利入口，
    但 db_path 以前默认是 runtime_data/erp_demo.db，于是覆盖分支
    （_resolve_databases）对任何叫 erp_db 的库**无条件生效**——接真实业务库时，
    语义层里配好的 erp_db 连接会被静默换成内置的演示文件。SQL 照跑、有结果、
    结果来自错误的库，是最难发现的一类错。

    演示路径不受影响：演示语义层自己就声明了同一个 erp_demo.db 路径，
    这个默认值本来就是重复的。
    """
    sample_size: int = 5
    # 多库配置；None 时按语义层里声明的库连接（默认是四个 Demo 库）
    databases: Optional[List[DatabaseConfig]] = None

    semantic_layer: Optional[Any] = None
    """
    业务语义层（ai_sql_agent.semantic.SemanticLayer）。

    None 时按 AI_SQL_SEMANTIC_DIR 解析：配了目录读目录，没配用内置演示元数据。
    显式传入主要用于测试与「同一进程里跑两套元数据」这种场景。

    类型写成 Any 是为了不让 objects 模块反向依赖 ai_sql_agent.semantic——
    那会绕成 pipeline → semantic → demo_data → pipeline 的循环导入。
    """
    # 单次查询 ~8k Token 预算
    prompt_budget: Optional[PromptBudgetConfig] = None
    # SQL 执行治理：单条查询最大返回行数与最长执行时间。API 服务按 AI_SQL_MAX_ROWS /
    # AI_SQL_MAX_EXECUTION_SECONDS 填这两项（api/app.py），直接构造时用这里的默认值。
    # 只对进程内执行器生效：use_mcp_protocol=True 时上限由 MCP server 进程自己定。
    max_rows: int = 1000
    max_execution_seconds: float = 10.0
    # 结果的字节预算与 SQLite 单值长度上限（AI_SQL_MAX_RESULT_BYTES / AI_SQL_MAX_VALUE_BYTES）。
    # 默认值与 Settings、两个执行器各自的默认值相同，数值钉在测试里。
    max_result_bytes: int = 8 * 1024 * 1024
    max_value_bytes: int = 8 * 1024 * 1024
    # 执行失败后的 Log-driven 修复回路
    enable_repair: bool = True
    enable_normalization: bool = True
    """问题归一化（代词消解 + 相对时间解析）开关。"""
    fiscal_year_start_month: int = 1
    """财年起始月份，影响「上季度」「去年」解析出的绝对区间。"""
    enable_short_path: bool = True
    """短路径开关：简单查询跳过独立规划调用，省约 1.5k tokens 与 2~3s。"""
    enable_cross_db_guard: bool = True
    """
    跨库越界静态校验：生成的 SQL 只能引用当前步骤目标库里的表，
    越界则带提示重生成一次。做成开关是为了能对照测量它的净收益。
    """

    enable_keyword_glossary: bool = True
    """
    抽词时附带业务术语表，让模型把口语说法映射到 Schema 已有的描述与别名上。

    做成开关和 enable_cross_db_guard 一样，是为了能对照测量它的净收益——
    它每次查询多占约 2k tokens，值不值要用留出集召回率的差值说话。
    """

    enable_cross_db_fewshot: bool = True
    """规划 Prompt 注入跨库范例（仅命中多库时）。做成开关是为了能消融归因。"""

    enable_plan_db_check: bool = True
    """步骤-库一致性校验 + 冲突时重新规划一次。做成开关是为了能消融归因。"""

    enable_intermediate_self_check: bool = True
    """
    中间步骤也做语义自检。

    代价是每个中间步骤多两次模型调用；收益是跨库第一步写错时能当场发现——
    第一步错了整条链路都错，而且错得静默（第二步照样跑通、照样返回数字）。
    """

    enable_validation: bool = True
    """软异常校验（基数校验 + 语义自检）总开关。"""
    enable_self_check: bool = True
    """语义自检开关。需要模型可用；离线或降级时自动跳过。"""
    max_repair_attempts: int = 2
    # True 时 SQL 执行走真实 MCP stdio 协议（ai_sql_agent.mcp_server 子进程）；
    # False 时使用进程内执行器，便于离线快速运行
    use_mcp_protocol: bool = False


@dataclass
class StepExecutionLog:
    """单个 CoT 步骤执行日志。"""

    database: str
    cot_step: object
    local_schema: str
    sql: str
    execution_request: Dict[str, str]
    execution_result: Dict[str, Any]
    repair_attempts: List[Dict[str, Any]] = field(default_factory=list)
    final_sql: str = ""
    """修复后实际成功执行的 SQL；未触发修复时与 sql 相同。"""

    validation: Dict[str, Any] = field(default_factory=dict)
    """
    软异常校验结论（空结果 / 基数异常 / 语义不一致）。

    与 repair_attempts 互补：repair 处理「SQL 跑不通」，validation 处理
    「SQL 跑通了但结果可能是错的」。
    """

    column_units: Dict[str, str] = field(default_factory=dict)
    """
    结果列 → 单位标识（契约 §4.6.5），如 `{"contract_amount": "USD"}`。

    判不出来的列不出现。**空 dict 表示「单位未知」，不表示「没有单位」**——
    调用方据此禁止补币种符号，尤其禁止回退到它自己的全局常量。
    """

    source_records: List[Dict[str, Any]] = field(default_factory=list)
    """
    来源记录标识（契约 §4.6.6）：这批数字是从哪几条业务记录读出来的。

    只有结果集真的带着主键列时才给得出来；给不出来时溯源手段仍是
    trace_id + final_sql。
    """


@dataclass
class PipelineResult:
    """端到端流程结果。"""

    query: str
    keywords: List[str]
    schema_context: str
    cot_output: str
    step_logs: List[StepExecutionLog] = field(default_factory=list)
    selected_databases: List[str] = field(default_factory=list)
    selected_tables: Dict[str, List[str]] = field(default_factory=dict)

    retrieved_columns: List[str] = field(default_factory=list)
    """
    检索命中的字段，形如 `pm_db.project.actual_cost`。

    暴露出来是为了让评测能算「字段召回率」——它与执行准确率相互独立：
    字段没召回到，后面生成再准也没用，两个指标要分开看才能定位瓶颈。
    """

    prompt_columns: List[str] = field(default_factory=list)
    """
    **实际进入 Prompt** 的字段（Token 预算裁剪之后）。

    与 retrieved_columns 的差值就是「检索召回了、但模型没看到」的部分。
    字段召回率必须按这一项算：检索到但没进 Prompt 的字段，对下游等于不存在。
    """

    normalization: Dict[str, Any] = field(default_factory=dict)
    """问题归一化结果（代词消解、时间区间解析）。"""

    degraded: bool = False
    """模型不可用时走了降级链路（重放历史 SQL 或直接给出降级答复）。"""

    degrade_reason: str = ""
    """降级原因，如 model_unavailable / model_unavailable_replayed_sql。"""

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "query": self.query,
            "keywords": self.keywords,
            "schema_context": self.schema_context,
            "cot_output": self.cot_output,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "step_logs": [
                {
                    "database": log.database,
                    "sql": log.sql,
                    "final_sql": log.final_sql or log.sql,
                    "execution_request": log.execution_request,
                    "execution_result": log.execution_result,
                    "repair_attempts": log.repair_attempts,
                }
                for log in self.step_logs
            ],
        }
