"""
ai-sql-agent 委派客户端。

客服这边答不了「我那张单子排到几号了」——答案在 ERP / MES / PM / WMS 四个
业务库里，不在知识库里。ai-sql-agent 是另一个已经跑起来的服务，能把这句
人话翻成只读 SQL 查出来。这个模块负责把问题递过去、把答案接回来。

**为什么直接调 `/query`，不走 A2A 协议。** 两边约好的 A2A 流式契约在独立
仓库 `a2a-contract` 的 `a2a-streaming-contract.md` 里（commit f33beb3）。那份
文档设计得很完整，但它描述的实现两边都还不存在：服务端没有 `ai_sql_a2a/`，
调用端也没有 client——契约跑在了实现前面。`/query` 是 ai-sql-agent 今天就有
的同步接口，鉴权、限流、审计、trace_id 一应俱全，先用它把链路打通。
将来换成 `message/stream` 时改的是本文件内部，注册出去的 Tool 接口不动。

**为什么不重试。** 查库路径实测 p90 17.7s（契约 §2，82 次真实调用），重试
一次就是把 17.7s 叠到用户等待上。失败直接当「没查到数据」，让 Agent 用知识
库和 SOP 回答或转人工。熔断和超时由 ToolManager 统一管，这里不自己做。

**超时为什么是 45s。** 契约 §1 给的是 25s，那个数字是给 A2A 的 `message/send`
定的过渡期预算——而 `send` 在契约里是**回滚路径**，掉查询的代价有限。这里走的
`/query` 是**主路径**，代价完全不同。

12 个真实问法的实测（演示库）：下游 13 次查询，中位 16.7s、
**最大 40.5s，3 次超过 25s**。也就是说 25s 会掉掉将近四分之一的订单查询，
而且掉得看不出来——下游那几次都成功返回了 200，只是调用方已经放弃。
契约 §2 自己的数据（p90 17.7s / max 68.3s）本来就预告了这件事。

45s 覆盖实测峰值，同时远低于契约给流式定的 90s 硬顶。代价是最坏情况客户多等
二十秒——换回来的是一个有系统数据的答案，而不是一段听起来合理的空话。

**这个数字是权宜的。** 真正的解法仍然是契约描述的流式：让用户在等待期间看到
"正在查 ERP 订单库"，那时候预算才谈得上放宽到 90s。在那之前，任何取值都是在
"掉数据"和"看空屏"之间挑一个不那么糟的。

**身份与数据权限（重要）。** 默认用一个固定的服务身份调过去
（`SQL_AGENT_USER_ID`，默认 `ai-helpdesk`），客户自己的 id 只用作 session，
隔离对面的会话记忆。这意味着**数据可见范围由 ai-sql-agent 上这把 key 的角色
策略决定，而不是由提问的客户决定**——给这把 key 配策略时要按「最不信任的
访客能看到什么」来配。需要按客户细分权限时，把对面的 key 设成
`can_impersonate: true`，再打开 `SQL_AGENT_IMPERSONATE`。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 实测覆盖：下游最大 40.5s（见模块开头）。契约 §1 的 25s 是给回滚路径 send 定的，
# 直连 /query 是主路径，套用它会掉掉约四分之一的查询。
DEFAULT_TIMEOUT_S = 45.0



# 哪些意图值得去查业务库。放在这里而不是 api 层，理由同 knowledge_base 的
# should_use_knowledge：/chat 主链路和评测 harness 必须共用同一套门控，
# 否则评测出来的分数和线上不是一回事。
#
# 选这六个的依据是 ai-sql-agent 手上有哪四个库（契约 §4.5）：
#   erp_db 销售订单/采购/回款 · mes_db 工单/工序/排产 · pm_db 项目/变更单 · wms_db 库存/出入库
# quote_pricing（报价）故意不在里面：报价是对外承诺，出口要走正式报价单，
# 不该由一句自然语言查询直接给数。要放开就配 SQL_AGENT_INTENTS，别改这里的默认值。
DEFAULT_DATA_INTENTS = frozenset({
    "order",          # 订单与交付（宽泛类）
    "order_status",   # 订单/生产进度与交期  → erp_db / mes_db
    "delivery",       # 送货、运输、到场排期  → erp_db / wms_db
    "order_change",   # 改单                  → pm_db
    "missing_part",   # 补件、缺件、错发      → wms_db
    "payment_issue",  # 定金/尾款/账款异常    → erp_db
})


def data_intents() -> frozenset[str]:
    """允许委派的意图集合，可用 SQL_AGENT_INTENTS 覆盖（逗号分隔）。"""
    raw = os.getenv("SQL_AGENT_INTENTS", "").strip()
    if not raw:
        return DEFAULT_DATA_INTENTS
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


# 宽泛意图：只说明"用户想查点什么/提个请求"，没落到细类。模型不可用、意图
# 识别只剩规则兜底时，订单问题几乎全落在这几个类里。
BROAD_INTENTS = frozenset({"query", "request", "complaint"})

# 能证明"这句话确实指向一张具体单据"的实体。意图分类没给出细类时，这是
# 第二个可信信号——它是规则提取的，不依赖模型，模型挂了它照样在。
BUSINESS_ENTITY_KEYS = ("order_id", "item_code")


def should_query_business_data(message: str, intent=None, entities=None) -> bool:
    """
    这条消息该不该去问 ai-sql-agent。

    两条通路：
      1. 意图落在数据类细分意图里 —— 正常路径；
      2. 意图只到宽泛类，但消息里带着订单号/物料号 —— 兜底路径。

    第 2 条存在的理由：意图识别是三路融合（LLM 70% / Embedding 20% / 规则 10%），
    LLM 那路不可用时结果会退到 `query` 这种宽泛类。这时候一句「订单号 SO-2024-006
    到哪了」的诉求明明是确定的，只因为分类退化就不查库，等于把降级的代价转嫁
    给了客户。实体是规则提取的，不依赖模型，正好补上这个洞。

    误判的代价是有限的：ai-sql-agent 自己还有一层路由，它认为不需要查库时会
    返回 queried_database=False，调用方据此丢弃（见 _build_order_data_context）。
    所以这里放宽一点，最坏是多一次下游调用，不会产出错误答案。
    """
    if not (message or "").strip():
        return False
    intent_value = getattr(intent, "value", intent)
    if intent_value in data_intents():
        return True
    if intent_value in BROAD_INTENTS:
        entities = entities or {}
        return any(entities.get(key) for key in BUSINESS_ENTITY_KEYS)
    return False

class SQLAgentClient:
    """ai-sql-agent `/query` 客户端。未配置 base_url 时 enabled=False。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        service_user_id: str = "ai-helpdesk",
        impersonate: bool = False,
        currency: str = "",
        transport: str = "query",
    ):
        self._base_url = (base_url or "").rstrip("/") or None
        self._api_key = api_key or None
        self._timeout_s = timeout_s
        self._service_user_id = service_user_id or "ai-helpdesk"
        self._impersonate = impersonate
        self._currency = (currency or "").strip()
        # query = 直连 /query（今天验证过的路）；a2a = 走 A2A（有流式进度，契约见 a2a-contract）
        self._transport = (transport or "query").strip().lower()
        self._a2a = None

    @classmethod
    def from_env(cls) -> SQLAgentClient:
        return cls(
            base_url=os.getenv("SQL_AGENT_URL", "").strip() or None,
            api_key=os.getenv("SQL_AGENT_API_KEY", "").strip() or None,
            timeout_s=float(os.getenv("SQL_AGENT_TIMEOUT_S", str(DEFAULT_TIMEOUT_S))),
            service_user_id=os.getenv("SQL_AGENT_USER_ID", "ai-helpdesk").strip(),
            impersonate=os.getenv("SQL_AGENT_IMPERSONATE", "false").lower() == "true",
            currency=os.getenv("SQL_AGENT_CURRENCY", "").strip(),
            transport=os.getenv("SQL_AGENT_TRANSPORT", "query").strip(),
        )

    @property
    def enabled(self) -> bool:
        return self._base_url is not None

    @property
    def currency(self) -> str:
        """
        下游金额的币种，未声明时为空串。

        为什么要单独声明：ai-sql-agent 的响应里**只有数字，没有币种**
        （`{"contract_amount": 103007.55}`），客服这边无从得知，于是在中文
        句子里默认写成「元」——接的是美国厂的库时，报给 builder 的金额就带了
        错误的币种。这不是能从数据里推断出来的东西，只能由部署方声明。

        留空则不声明，行为与加这个字段之前一致。
        """
        return self._currency

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/query" if self._base_url else ""

    async def query(
        self,
        question: str,
        user_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """
        问一次业务数据。

        返回归一化后的字段：answer / queried_database / selected_databases /
        has_risk / degraded / trace_id。

        失败**不吞**，直接抛——由 ToolManager 记熔断、走 fallback。在这里
        catch 掉会让熔断器永远看不到失败，一个挂掉的下游会被无限重敲。
        """
        if not self.enabled:
            raise RuntimeError("SQL_AGENT_URL 未配置，委派未启用")

        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        payload = {
            # impersonate 关闭时一律报服务身份：对面 key 若没有 can_impersonate，
            # 传客户自己的 id 会被 403 挡掉，整条链路直接失效。
            "user_id": (user_id or self._service_user_id) if self._impersonate else self._service_user_id,
            # 会话隔离用客服这边的 conv_id，让对面的多轮记忆按对话分开。
            "session_id": session_id or "helpdesk-oneshot",
            "query": question,
        }

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(self.endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, dict):
            raise ValueError(f"ai-sql-agent 返回了非对象响应: {type(data).__name__}")

        result = {
            "answer": str(data.get("answer", "")).strip(),
            "queried_database": bool(data.get("queried_database", False)),
            "selected_databases": data.get("selected_databases") or [],
            "has_risk": bool(data.get("has_risk", False)),
            "degraded": bool(data.get("degraded", False)),
            "degrade_reason": str(data.get("degrade_reason", "")),
            "trace_id": str(data.get("trace_id", "")),
            "route": str(data.get("route", "")),
        }

        # 对面的 trace_id 落进本方日志，两个服务的链路才能对起来——出了问题
        # 拿这个 id 直接去 ai-sql-agent 的审计库里查那一次调用。
        logger.info(
            "sql_agent.query 完成 trace_id=%s route=%s queried_db=%s databases=%s risk=%s degraded=%s",
            result["trace_id"], result["route"], result["queried_database"],
            result["selected_databases"], result["has_risk"], result["degraded"],
        )
        return result

    @property
    def transport(self) -> str:
        return self._transport

    def _a2a_client(self):
        if self._a2a is None:
            from tools.a2a_client import a2a_client_from_env
            self._a2a = a2a_client_from_env(self._base_url or "", self._api_key)
        return self._a2a

    async def query_a2a(self, question: str, user_id: str = "", session_id: str = "",
                        on_progress=None) -> dict[str, Any]:
        """
        走 A2A。Card 说支持流式就先试 stream；服务端在**流打开之前**返回 -32004
        （开关关着）就改发 send——这不算重试，一个令牌都还没扣（契约 §5.4 的豁免）。
        流中途的任何失败都直接抛，**不**重发 send：那时配额已经扣了、模型已经跑了，
        重发就是实打实的两份。
        """
        from tools.a2a_client import StreamUnsupported
        client = self._a2a_client()
        uid = (user_id or self._service_user_id) if self._impersonate else self._service_user_id
        sid = session_id or "helpdesk-oneshot"
        if await client.streaming_supported():
            try:
                result = await client.stream(question, uid, sid, on_progress=on_progress)
            except StreamUnsupported:
                logger.info("A2A 服务端返回 -32004（流打开之前），改发 message/send")
                result = await client.send(question, uid, sid)
        else:
            result = await client.send(question, uid, sid)
        logger.info("sql_agent.a2a 完成 trace_id=%s route=%s queried_db=%s databases=%s",
                    result["trace_id"], result["route"], result["queried_database"],
                    result["selected_databases"])
        return result

    async def handler(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        """作为工具 `order_data_query` 的 handler 注册。"""
        question = params.get("query", "")
        user_id = params.get("user_id", "")
        session_id = params.get("session_id", "")
        if self._transport == "a2a":
            on_progress = (context or {}).get("progress_cb") if isinstance(context, dict) else None
            return await self.query_a2a(question, user_id, session_id, on_progress=on_progress)
        return await self.query(question=question, user_id=user_id, session_id=session_id)
