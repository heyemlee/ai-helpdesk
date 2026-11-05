"""
业务语义层：模型认识业务的唯一入口。

三层检索靠这里的描述与别名把口语问法映射到字段，规划靠跨库关联键拆步，
对话问答靠指标定义解释口径。以前这些只为四个演示库硬编码在
`ai_sql_agent.pipeline.demo_data` 里，接真实业务库时元数据为空——检索质量直接塌掉，
现象却是「模型答不准」。现在它是可编辑的配置：一个库一个 YAML，
业务人员能改，CI 能校验，接哪套库就写哪套元数据。
"""

from .loader import demo_semantic_layer, load_semantic_dir, write_semantic_dir
from .objects import DatabaseSemantics, SemanticLayer
from .settings_bridge import active_semantic_layer, resolve_semantic_layer
from .validator import (
    ValidationIssue,
    ValidationReport,
    coverage_summary,
    validate_semantic_layer,
)

__all__ = [
    "DatabaseSemantics",
    "SemanticLayer",
    "ValidationIssue",
    "ValidationReport",
    "active_semantic_layer",
    "coverage_summary",
    "demo_semantic_layer",
    "load_semantic_dir",
    "resolve_semantic_layer",
    "validate_semantic_layer",
    "write_semantic_dir",
]
