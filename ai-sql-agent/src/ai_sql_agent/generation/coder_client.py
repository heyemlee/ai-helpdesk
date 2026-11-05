from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ai_sql_agent.core.model_client import FollowsModelSettings


@dataclass
class CoderModelConfig:
    """
    Coder 模型配置。

    协议由 provider 决定：openai（OpenAI 兼容）或 anthropic（Messages）。
    留空的字段在构造 Client 时从全局 Settings 补齐（见 ai_sql_agent.core.settings），
    之后跟着页面保存的模型连接走。
    没有 API Key 时默认走 Mock，方便先跑通流程。
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = ""
    temperature: float = 0.0
    timeout: Optional[float] = None
    use_mock_when_no_api_key: bool = True


class CoderModelClient(FollowsModelSettings):
    """
    Coder 模型 Client。

    HTTP 调用统一走 ai_sql_agent.core.ModelTransport：超时、限流退避重试、
    错误分类与 Token 用量统计都在那里处理。
    """

    COMPONENT = "coder"
    MODEL_SETTING = "coder_model"

    def __init__(self, config: CoderModelConfig | None = None):
        self._bind_connection(config or CoderModelConfig())

    def generate_sql(self, prompt: str) -> str:
        """
        生成 SQL。
        """
        connection = self._connection()
        if connection.has_key:
            return connection.transport.chat(
                connection.url,
                [{"role": "user", "content": prompt}],
                model=connection.model,
                temperature=self.config.temperature,
            )

        if self.config.use_mock_when_no_api_key:
            return self._mock_generate(prompt)

        raise ValueError(
            "缺少模型 API Key（OPENAI_API_KEY / ANTHROPIC_API_KEY），无法调用 Coder 模型。"
        )

    def _mock_generate(self, prompt: str) -> str:
        """Mock SQL 生成，仅用于离线跑通流程。"""
        # 跨库第二步：把上一步的 project_code 回填成 ERP 的 job_no
        if "purchase_order" in prompt and "# 上一步结果" in prompt:
            codes = re.findall(r'"project_code":\s*"([^"]+)"', prompt)
            if codes:
                in_list = ", ".join(f"'{c}'" for c in dict.fromkeys(codes))
                return (
                    "SELECT job_no, SUM(po_amt) AS total_po_amt\n"
                    "FROM purchase_order\n"
                    f"WHERE job_no IN ({in_list})\n"
                    "GROUP BY job_no;"
                )

        # 跨库第一步 / 单库：超预算项目
        if "actual_cost" in prompt and "budget_amt" in prompt:
            target = prompt.split("输出目标：")[-1].split("\n")[0] if "输出目标：" in prompt else ""
            field = "project_code" if "project_code" in target else "project_name"
            return f"SELECT {field}\nFROM project\nWHERE actual_cost > budget_amt;"

        # 库存低于安全库存
        if "on_hand_qty" in prompt and "safety_stock" in prompt:
            return """SELECT material.material_name
FROM material
JOIN stock_snapshot
  ON material.material_code = stock_snapshot.material_code
WHERE stock_snapshot.on_hand_qty < material.safety_stock;"""

        # 供应商准时率
        if "on_time_rate" in prompt:
            m = re.search(r"(0\.\d+)", prompt)
            threshold = m.group(1) if m else "0.9"
            return f"SELECT supplier_name\nFROM supplier\nWHERE on_time_rate < {threshold};"

        return "SELECT 1;"

    def clean_sql(self, text: str) -> str:
        """
        清理模型输出，只保留一条 SQL。

        处理：
        - 有 Markdown 代码块就只取第一个代码块里的内容，块前块后的说明不要；
        - 去掉“输出SQL语句：”之类的前缀；
        - 从第一个 SELECT/WITH 开始，到第一个不在引号里的分号为止——每一步只允许一条
          语句，分号后面多半是模型补的一句解释；没有分号时，空行之后以中文开头的段落
          也是解释，不是 SQL。
        """
        text = text.strip()

        fenced = _FENCED_BLOCK.search(text)
        if fenced and fenced.group(1).strip():
            text = fenced.group(1).strip()
        else:
            text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text, flags=re.I)

        text = re.sub(
            r"^(输出SQL语句|SQL语句|SQL)\s*[:：]\s*",
            "",
            text,
            flags=re.I,
        ).strip()

        match = re.search(r"\b(?:SELECT|WITH)\b", text, flags=re.I)
        sql = _first_statement(text[match.start():] if match else text).strip()

        if sql and not sql.endswith(";"):
            sql += ";"

        return sql


_FENCED_BLOCK = re.compile(
    r"```[ \t]*(?:sql|sqlite|postgres|postgresql|pgsql)?[ \t]*\n(.*?)```", re.I | re.S
)
_PROSE_AFTER_BLANK_LINE = re.compile(r"\n[ \t]*\n[ \t]*[\u4e00-\u9fff]")


def _first_statement(text: str) -> str:
    """取到第一个不在引号、注释里的分号（含）为止；没有就取到第一段中文说明之前。"""
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                # 'it''s' 里连着的两个引号是转义，不是字符串结束
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
        elif char in ("'", '"'):
            quote = char
        elif text.startswith("--", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif char == ";":
            return text[: index + 1]
        index += 1

    prose = _PROSE_AFTER_BLANK_LINE.search(text)
    return text[: prose.start()] if prose else text
