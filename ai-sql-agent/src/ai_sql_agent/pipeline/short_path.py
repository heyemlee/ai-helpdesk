from __future__ import annotations

import re
from dataclasses import dataclass


# 多步信号：命中任一即认为需要完整规划
_MULTI_STEP_SIGNALS = (
    # 跨库/跨域
    "跨库", "各个系统", "对比",
    # 聚合后过滤（HAVING 类，典型多步）
    "超过平均", "高于平均", "低于平均", "占比", "同比", "环比",
    # 需要先算再筛
    "然后", "再按", "之后", "并且统计", "分别统计",
)

# 单步信号：单表查找/简单过滤
_SIMPLE_SIGNALS = ("有哪些", "是多少", "列出", "查询", "看看", "多少个", "数量")


@dataclass
class RoutingDecision:
    """短路径判定结果。"""

    use_short_path: bool
    reason: str

    def to_dict(self) -> dict:
        return {"use_short_path": self.use_short_path, "reason": self.reason}


def decide_short_path(
    query: str,
    *,
    selected_tables: dict[str, list[str]] | None = None,
    enabled: bool = True,
) -> RoutingDecision:
    """
    判断能否跳过独立的规划模型调用，直接生成 SQL。

    收益：省掉一次推理模型调用，约 -1.5k tokens、-2~3s 延迟。
    风险：多步问题走短路径会显著掉准确率。

    因此判定是**保守**的——只有明确像单步查询才走短路径，任何多步信号
    或跨库迹象都退回完整链路。宁可多花一次调用，也不要答错。
    """
    if not enabled:
        return RoutingDecision(False, "短路径已关闭")

    if any(signal in query for signal in _MULTI_STEP_SIGNALS):
        return RoutingDecision(False, "命中多步信号，需要完整规划")

    # 检索命中多个库 → 大概率跨库，必须规划
    if selected_tables:
        databases = [db for db, tables in selected_tables.items() if tables]
        if len(databases) > 1:
            return RoutingDecision(False, f"检索命中 {len(databases)} 个库，需要跨库规划")

        total_tables = sum(len(tables) for tables in selected_tables.values())
        if total_tables > 3:
            return RoutingDecision(
                False, f"候选表 {total_tables} 张，关联关系复杂，走完整规划"
            )

    # 含显式多条件连接词时也退回
    if len(re.findall(r"并且|同时|以及", query)) >= 2:
        return RoutingDecision(False, "多重条件，走完整规划")

    if any(signal in query for signal in _SIMPLE_SIGNALS):
        return RoutingDecision(True, "单库单表简单查找，跳过规划调用")

    return RoutingDecision(False, "无法确认为简单查询，保守走完整规划")
