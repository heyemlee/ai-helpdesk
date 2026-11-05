from __future__ import annotations

import math
import re
from dataclasses import dataclass


def estimate_tokens(text: str) -> int:
    """估算文本 Token 数：中文按字计，其余按 4 字符 1 Token。"""
    if not text:
        return 0
    chinese = len(re.findall(r"[一-鿿]", text))
    remaining = len(re.sub(r"[一-鿿\s]", "", text))
    return max(1, chinese + math.ceil(remaining / 4))


def trim_to_tokens(text: str, max_tokens: int, keep: str = "head") -> str:
    """
    按行裁剪文本直到满足 Token 预算。

    keep="head" 保留开头（适合按相关性排序的 Schema 上下文）；
    keep="tail" 保留结尾（适合会话记忆，最近的轮次最重要）。
    """
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    lines = text.splitlines()
    if keep == "tail":
        lines = list(reversed(lines))

    selected: list[str] = []
    total = 0
    for line in lines:
        line_tokens = estimate_tokens(line)
        if selected and total + line_tokens > max_tokens:
            break
        if not selected and line_tokens > max_tokens:
            # 单行超预算时按字符比例截断兜底
            ratio = max_tokens / line_tokens
            cut = max(1, int(len(line) * ratio))
            line = line[-cut:] if keep == "tail" else line[:cut]
            line_tokens = estimate_tokens(line)
        selected.append(line)
        total += line_tokens

    if keep == "tail":
        selected = list(reversed(selected))
    return "\n".join(selected)


@dataclass
class PromptBudgetConfig:
    """
    单次查询 ~8k Token 预算的分配。

    total_budget 覆盖记忆上下文、Schema 上下文、CoT 规划与固定指令；
    generation_reserved 预留给模型输出，不计入拼接内容。
    """

    total_budget: int = 8000
    memory_budget: int = 2000
    schema_budget: int = 3200
    plan_budget: int = 1400
    generation_reserved: int = 1400

    def __post_init__(self) -> None:
        allocated = self.memory_budget + self.schema_budget + self.plan_budget
        if allocated + self.generation_reserved > self.total_budget:
            raise ValueError("各部分预算之和不能超过 total_budget")


class PromptBudgetManager:
    """在流程各拼接点强制执行 Token 预算。"""

    def __init__(self, config: PromptBudgetConfig | None = None):
        self.config = config or PromptBudgetConfig()

    def fit_memory(self, text: str) -> str:
        return trim_to_tokens(text, self.config.memory_budget, keep="tail")

    def fit_schema_sections(self, sections: list, separator: str = "\n\n") -> str:
        """
        **按库**分配 Schema 预算，而不是把拼好的整段做头部截断。

        为什么不能整段截断：Schema 上下文是按库顺序拼接的，头部截断等于
        优先把**最后一个库整段砍掉**——而跨库查询恰恰依赖第二个库。
        实测一条跨库查询有 10 个已召回字段被裁掉，规划模型随后在输出里写
        「Schema 未提供"当前生产工序"对应字段及完整表结构」，然后生成了
        `SELECT job_no, NULL AS current_production_process`。

        分配方式是**按需求比例**的水填充，不是均分：用不满配额的库让出余量，
        剩下的预算按各库实际 Schema 体量的比例分配，迭代到稳定。

        为什么不能均分（这是实测踩出来的）：库级检索会保留 2 个库，单库查询里
        第二个往往是捎带进来的噪声库。均分等于把主库预算砍掉一半去养一个用不上的库
        ——实测单库准确率 100% → 88%、进 Prompt 召回率 99.3% → 92.7%，
        11 条题的 gold 字段被裁掉，其中 3 条直接生成了 `NULL AS xxx`。
        为了保护第二个库把第一个库饿死，只是换了个偏置。
        """
        sections = [section for section in sections if section and section.strip()]
        if not sections:
            return ""

        budget = self.config.schema_budget
        costs = [estimate_tokens(section) for section in sections]
        if sum(costs) <= budget:
            return separator.join(sections)

        # 分隔符本身也占预算
        budget = max(1, budget - estimate_tokens(separator) * (len(sections) - 1))

        # 1) 保底：每个库先拿一份最低额度，保证不会被整段砍掉；
        #    本来就用不满的库到此已经拿全，不参与后面的分配。
        floor = max(1, budget // (4 * len(sections)))
        quotas = [min(cost, floor) for cost in costs]

        # 2) 余量按**剩余需求**比例分，而不是均分：
        #    单库查询里第二个库通常很小，它拿走自己那点就够了，
        #    剩下的全归主库；真正的跨库查询两库体量相当，才会接近对半。
        remaining = budget - sum(quotas)
        residual = [cost - quota for cost, quota in zip(costs, quotas)]
        total_residual = sum(residual)
        if remaining > 0 and total_residual > 0:
            for i, need in enumerate(residual):
                quotas[i] += remaining * need // total_residual

        return separator.join(
            trim_to_tokens(section, quotas[i], keep="head")
            for i, section in enumerate(sections)
            if quotas[i] > 0
        )


def columns_in_prompt(schema_context: str) -> list:
    """
    从**裁剪后**的 Schema 文本里解析出模型真正看得到的字段，形如 `db.table.column`。

    为什么需要它：Schema 上下文要过 Token 预算裁剪（`fit_schema_sections`），超出预算的部分
    直接消失，可能把一张表从中间切断。而字段召回率一直是拿检索输出
    （`retrieved_columns`，裁剪**之前**）算的——检索召回了、但被预算裁掉、
    模型压根没看到的字段，也算进了召回率。

    实测：一条跨库查询检索命中 46 个字段，其中 10 个被预算裁掉，召回率仍报 100%，
    而规划模型在输出里写「Schema 未提供完整表结构」。指标和模型看到的东西对不上，
    这种召回率越高越危险——它掩盖的正是「模型没拿到字段」这个真实失败原因。
    """
    columns: list = []
    database = table = ""
    # 最后一行可能被截断在一半，不完整的行一律不算数
    lines = schema_context.split("\n")
    if schema_context and not schema_context.endswith("\n"):
        lines = lines[:-1]

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("数据库："):
            database = stripped[len("数据库："):].strip()
            table = ""
        elif stripped.startswith("表名："):
            table = stripped[len("表名："):].strip()
        elif stripped.startswith("- 字段名：") and database and table:
            column = stripped[len("- 字段名："):].strip()
            if column:
                columns.append(f"{database}.{table}.{column}")
    return columns
