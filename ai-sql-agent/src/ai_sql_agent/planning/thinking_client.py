from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from ai_sql_agent.core.model_client import FollowsModelSettings


@dataclass
class ThinkingModelConfig:
    """
    思考模型配置。

    协议由 provider 决定：openai（OpenAI 兼容）或 anthropic（Messages）。
    留空字段在构造 Client 时从全局 Settings 补齐，之后跟着页面保存的模型连接走。
    没有 API Key 时默认走 Mock，方便先跑通流程。
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = ""
    temperature: float = 0.0
    timeout: Optional[float] = None
    use_mock_when_no_api_key: bool = True
    mock_stream_delay: float = 0.02


class ThinkingModelClient(FollowsModelSettings):
    """
    思考模型 Client。

    用于将用户 Query 和 Schema 图转换为结构化 CoT 规划结果。
    支持普通输出和流式输出，HTTP 治理统一走 ai_sql_agent.core.ModelTransport。
    """

    COMPONENT = "thinking"

    def __init__(self, config: ThinkingModelConfig | None = None):
        self._bind_connection(config or ThinkingModelConfig())

    def answers_from_mock(self) -> bool:
        """
        现在调用的话，回答是不是 Mock 编的：没有 Key，且允许走 Mock。

        跟着页面保存的模型连接走（`model_available` 会先刷新连接）。
        语义自检靠它区分「模型的回答读不出结论」和「根本没有模型」。
        """
        return not self.model_available() and self.config.use_mock_when_no_api_key

    def generate(self, prompt: str) -> str:
        """
        非流式生成规划结果。
        """
        connection = self._connection()
        if connection.has_key:
            return connection.transport.chat(
                connection.url,
                self._messages(prompt),
                model=connection.model,
                temperature=self.config.temperature,
            )

        if self.config.use_mock_when_no_api_key:
            return self._mock_generate(prompt)

        raise ValueError(
            "缺少模型 API Key（OPENAI_API_KEY / ANTHROPIC_API_KEY），无法调用思考模型。"
        )

    def generate_structured(self, prompt: str, schema: dict, name: str) -> str:
        """
        要求按 JSON Schema 输出的一次生成，返回 JSON 文本。

        OpenAI 兼容协议由服务端按 Schema 约束（strict）；Anthropic 和不支持的中转站
        照常生成，靠提示词里写明的 JSON 格式。没有 Key 时走 Mock，和 `generate` 一样。
        """
        connection = self._connection()
        if connection.has_key:
            return connection.transport.chat(
                connection.url,
                self._messages(prompt),
                model=connection.model,
                temperature=self.config.temperature,
                response_schema=schema,
                schema_name=name,
            )
        return self.generate(prompt)

    def generate_stream(self, prompt: str) -> Iterator[str]:
        """
        流式生成规划结果。

        返回一个迭代器，每次 yield 一小段文本。
        Demo 中可以边生成边 print。
        """
        connection = self._connection()
        if connection.has_key:
            # 流式（SSE，OpenAI / Anthropic 两种事件格式都能解析）
            yield from connection.transport.chat_stream(
                connection.url,
                self._messages(prompt),
                model=connection.model,
                temperature=self.config.temperature,
            )
            return

        if self.config.use_mock_when_no_api_key:
            yield from self._mock_generate_stream(prompt)
            return

        raise ValueError(
            "缺少模型 API Key（OPENAI_API_KEY / ANTHROPIC_API_KEY），无法调用思考模型。"
        )

    def _messages(self, prompt: str) -> list:
        return [{"role": "user", "content": prompt}]

    @staticmethod
    def _extract_query(prompt: str) -> str:
        """从 Prompt 中取出用户问题。

        Mock 必须按「问题」分支而不是按 Schema 内容分支——四个库的 Schema 都在
        Prompt 里时，按字段名匹配会串到别的分支上。
        """
        match = re.search(r"#\s*用户Query\s*\n(.+?)(?:\n\s*#|\Z)", prompt, re.S)
        return match.group(1).strip() if match else prompt

    def _mock_generate(self, prompt: str) -> str:
        """
        Mock 规划结果。

        每步带「预期输出量级」，供执行后的基数校验比对。
        """
        query = self._extract_query(prompt)
        has = lambda *ws: any(w in query for w in ws)

        # 跨库：项目库定位超预算项目 → ERP 按 job_no 归集采购金额
        if has("超预算", "超出预算", "实际成本") and has("采购"):
            return """步骤1：
(
  数据库: pm_db,
  处理对象: project.actual_cost，project.budget_amt，project.project_code,
  操作指令: 先在project表中筛选actual_cost大于budget_amt的项目；最后输出这些项目的project_code,
  输出目标: project.project_code,
  预期输出量级: 数条
)
步骤2：
(
  数据库: erp_db,
  处理对象: purchase_order.job_no，purchase_order.po_amt,
  操作指令: 先读取上一步结果中的project_code列表；再按命名约定对应到purchase_order的job_no；最后按job_no汇总po_amt,
  输出目标: purchase_order.po_amt,
  预期输出量级: 数条
)"""

        # 单库：超预算项目
        if has("超预算", "超出预算", "实际成本"):
            return """步骤1：
(
  数据库: pm_db,
  处理对象: project.actual_cost，project.budget_amt，project.project_name,
  操作指令: 在project表中筛选actual_cost大于budget_amt的记录，输出project_name,
  输出目标: project.project_name,
  预期输出量级: 数条
)"""

        # 单库：库存低于安全库存
        if has("安全库存", "库存不足", "缺货"):
            return """步骤1：
(
  数据库: wms_db,
  处理对象: stock_snapshot.on_hand_qty，material.safety_stock，material.material_name，material.material_code ↔ stock_snapshot.material_code,
  操作指令: 先关联material与stock_snapshot表；再筛选on_hand_qty小于safety_stock的记录；最后输出material_name,
  输出目标: material.material_name,
  预期输出量级: 数条
)"""

        # 单库：供应商准时率
        if has("准时率", "准时交付", "供应商"):
            return """步骤1：
(
  数据库: erp_db,
  处理对象: supplier.on_time_rate，supplier.supplier_name,
  操作指令: 在supplier表中筛选on_time_rate低于阈值的记录，输出supplier_name,
  输出目标: supplier.supplier_name,
  预期输出量级: 数条
)"""

        # 单库：生产工序
        if has("工序", "生产进度", "做到哪"):
            return """步骤1：
(
  数据库: mes_db,
  处理对象: work_order.current_process，work_order.job_no,
  操作指令: 在work_order表中输出各工单的job_no与current_process,
  输出目标: work_order.current_process,
  预期输出量级: 数条
)"""

        return """步骤1：
(
  数据库: 缺失,
  处理对象: Schema中未找到足够的表、字段或表关联关系,
  操作指令: 先检查用户Query涉及的筛选字段、输出字段和表关联关系；再发现当前Schema无法完整支撑该查询；最后返回缺失信息说明,
  输出目标: 缺失，无法生成明确输出目标,
  预期输出量级: 未知
)"""

    def _mock_generate_stream(self, prompt: str) -> Iterator[str]:
        """
        Mock 流式输出。

        为了让 Demo 看起来像真实流式输出，这里按字符逐步 yield。
        """
        text = self._mock_generate(prompt)

        for char in text:
            yield char
            time.sleep(self.config.mock_stream_delay)
