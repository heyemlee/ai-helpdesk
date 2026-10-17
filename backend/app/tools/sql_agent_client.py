"""
ai-sql-agent 委派客户端。

客服这边答不了「我那张单子排到几号了」——答案在 ERP、生产、交付和仓储这些
业务库里，不在知识库里。ai-sql-agent 是另一个已经跑起来的服务，能把这句
人话翻成只读 SQL 查出来。这个模块负责把问题递过去、把答案接回来。

**两条 transport，默认 `/query`。** 两边的约定在独立仓库 `a2a-contract` 的
`a2a-streaming-contract.md` 里（v1.13，tag v1.13 / commit faa699f）。`/query` 是
ai-sql-agent 的同步接口，鉴权、限流、审计、trace_id 一应俱全，链路最早就是靠它
打通的；`SQL_AGENT_TRANSPORT=a2a` 时改走 A2A（`message/stream` / `message/send`，
客户端在 `a2a_client.py`），查询阶段能实时传回前端。默认仍是 `query`：它跑得久、
验得多（契约附录 B）；切 A2A 时超时要一起调，见下文。三个承载者共用同一套结果
语义和信任判定（契约 §4.4.1，v1.8 起），所以换 transport 不换证据标准，注册出去的
Tool 接口也不动。

**为什么不重试。** 查库路径实测 p90 17.7s（契约 §2，82 次真实调用），重试
一次就是把 17.7s 叠到用户等待上。失败直接当「没查到数据」，让 Agent 用知识
库和 SOP 回答或转人工。熔断和超时由 ToolManager 统一管，这里不自己做。

**超时为什么是 45s。** 契约 §1 最早给的是 25s，那个数字是给 A2A 的 `message/send`
定的过渡期预算——而 `send` 在契约里是**回滚路径**，掉查询的代价有限。这里走的
`/query` 是**主路径**，代价完全不同。

12 个真实问法的实测（演示库）：下游 13 次查询，中位 16.7s、
**最大 40.5s，3 次超过 25s**。也就是说 25s 会掉掉将近四分之一的订单查询，
而且掉得看不出来——下游那几次都成功返回了 200，只是调用方已经放弃。
契约 §2 自己的数据（p90 17.7s / max 68.3s）本来就预告了这件事。

45s 覆盖实测峰值，同时远低于契约给流式定的 90s 硬顶。代价是最坏情况客户多等
二十秒——换回来的是一个有系统数据的答案，而不是一段听起来合理的空话。
契约 v1.6 把 send 预算补成了三档（§1③：流式未实现 25s、已实现但没承接流量 45s、
承接流量后 90s），45s 正好是今天这一档：默认 transport 还是 `query`。

**再放宽要先让用户看到进度。** 流式已经有了，走 A2A 时用户在等待期间能看到
"正在查 ERP 订单库"。等生产流量真的切到 A2A 流式，才进第三档、send 放宽到 90s；
那时 `SQL_AGENT_TIMEOUT_S` 要一起抬：外层工具超时 = 它 + 2s，必须大于 send 预算
（`core/preflight.py` 启动时检查），也要盖住流式 90s 的硬顶（这条没有启动检查）。
还走 `/query` 的时候，任何取值都仍是在"掉数据"和"看空屏"之间挑一个不那么糟的。

**身份与数据权限（重要）。** 默认用一个固定的服务身份调过去
（`SQL_AGENT_USER_ID`，默认 `ai-helpdesk`），客户自己的 id 只用作 session，
隔离对面的会话记忆。这意味着**数据可见范围由 ai-sql-agent 上这把 key 的角色
策略决定，而不是由提问的客户决定**——给这把 key 配策略时要按「最不信任的
访客能看到什么」来配。需要按客户细分权限时，把对面的 key 设成
`can_impersonate: true`，再打开 `SQL_AGENT_IMPERSONATE`。
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import httpx

from app.core.evidence import assess, normalize_delegation_result

logger = logging.getLogger(__name__)


def _downstream_session_id(user_id: str, session_id: str) -> str:
    """
    下游会话标识 = 认证身份 + 本方会话 id 的摘要。

    **为什么不能直接把 conv_id 发过去。** 默认配置下所有查询都以同一个服务身份
    发出（`SQL_AGENT_USER_ID`），下游的多轮记忆就只按 `session_id` 分隔。
    而 `conv_id` 是**请求体里客户端自己填的**（`api/main.py` 的 `ChatRequest`）：
    照原样转发的话，客户 A 把 conv_id 改成客户 B 的那一个，就能让 B 的下游
    会话记忆注入自己这轮的上下文。这不是"权限没做细"，是一条**跨客户串话**
    的通道——而且它没有任何症状，两边日志看起来都完全正常。

    绑进 `user_id`（`resolve_user_id` 推导出来的可信身份，不是请求体里那个）
    之后，两个用户不可能落到同一个下游会话上，也无法指定别人的会话。
    空 `session_id` 也照样绑：原先它们共用一个字面量 `"helpdesk-oneshot"`，
    那是同一个问题最严重的形态——**所有**没带 conv_id 的用户共享一段记忆。

    取摘要而不是拼接，是为了不让本方的 user_id 和 conv_id 出现在另一个服务的
    存储里。代价是下游日志里看不到原始 conv_id，所以本方日志要记住这个映射
    （`sql_agent.query 完成` 那一行带 session=）。
    """
    material = f"{user_id or 'anonymous'}\x00{session_id or 'oneshot'}".encode()
    return f"hd-{hashlib.sha256(material).hexdigest()[:32]}"

# 实测覆盖：下游最大 40.5s（见模块开头），也正好是契约 §1③ 今天这一档的 send 预算。
# 25s 是流式还没实现时给回滚路径 send 的那一档；直连 /query 是主路径，
# 套用它会掉掉约四分之一的查询。
DEFAULT_TIMEOUT_S = 45.0



# 哪些意图值得去查业务库。放在这里而不是 api 层，理由同 knowledge_base 的
# should_use_knowledge：/chat 主链路和评测 harness 必须共用同一套门控，
# 否则评测出来的分数和线上不是一回事。
#
# 选这六个的依据是 ai-sql-agent 手上有哪些库。契约 §4.5 从 v1.6 起只钉标识符的形状，
# 具体有哪几个以它的 Agent Card 为准；今天是这四个（mes_db 是 prod_db 的旧名，
# pm_db 的职责本就在 erp_db，两个都已退役）：
#   erp_db 工程/变更单/应收与回款 · prod_db 生产工单/工序/质检 · svc_db 送货/售后/补件 · wms_db 供应商/物料/到货与库存
# quote_pricing（报价）故意不在里面：报价是对外承诺，出口要走正式报价单，
# 不该由一句自然语言查询直接给数。要放开就配 SQL_AGENT_INTENTS，别改这里的默认值。
DEFAULT_DATA_INTENTS = frozenset({
    "order",          # 订单与交付（宽泛类）
    "order_status",   # 订单/生产进度与交期  → erp_db / prod_db
    "delivery",       # 送货、运输、到场排期  → svc_db
    "order_change",   # 改单                  → erp_db（变更单）
    "missing_part",   # 补件、缺件、错发      → svc_db / wms_db
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
    返回 queried_database=False，调用方据此丢弃（见 turn_tools.delegate_order_query）。
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
    """ai-sql-agent 委派客户端：默认走 `/query`，`SQL_AGENT_TRANSPORT=a2a` 时走 A2A。未配置 base_url 时 enabled=False。"""

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
            # 会话隔离：**绑定到认证身份之后**再发下去，见 _downstream_session_id。
            "session_id": _downstream_session_id(user_id, session_id),
            "query": question,
        }

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(self.endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, dict):
            raise ValueError(f"ai-sql-agent 返回了非对象响应: {type(data).__name__}")

        # 归一与 A2A 那两条共用同一个函数，信任判定因此只有一份口径。
        # 契约 §4.4.1（v1.8）把直连 `/query` 正式纳入保护范围：契约面是
        # data part 本身，与谁承载无关，三个承载者同一套判据。
        # 逐步骤日志的两个名字（`step_logs` / `steps`）在归一里一并处理，
        # 过渡期两个都认、`steps` 优先——见 core.evidence._steps_field。
        result = normalize_delegation_result(data)

        # 对面的 trace_id 落进本方日志，两个服务的链路才能对起来——出了问题
        # 拿这个 id 直接去 ai-sql-agent 的审计库里查那一次调用。
        # 一并记信任结论：`queried_database=true` 而 `execution_status=failed`
        # 是"SQL 全跑挂了但下游照发成功"那种形态，只看前者在日志里是看不出来的。
        v = assess(result)
        logger.info(
            "sql_agent.query 完成 trace_id=%s session=%s route=%s queried_db=%s databases=%s "
            "risk=%s degraded=%s status=%s(%s) usable=%s",
            result["trace_id"], payload["session_id"], result["route"], v.queried_database,
            result["selected_databases"], v.has_risk, v.degraded,
            v.execution_status, v.status_source, v.usable_as_source,
        )
        return result

    @property
    def transport(self) -> str:
        return self._transport

    def _a2a_client(self):
        if self._a2a is None:
            from app.tools.a2a_client import a2a_client_from_env
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
        from app.tools.a2a_client import StreamUnsupported
        client = self._a2a_client()
        uid = (user_id or self._service_user_id) if self._impersonate else self._service_user_id
        sid = _downstream_session_id(user_id, session_id)
        if await client.streaming_supported():
            try:
                result = await client.stream(question, uid, sid, on_progress=on_progress)
            except StreamUnsupported:
                logger.info("A2A 服务端返回 -32004（流打开之前），改发 message/send")
                result = await client.send(question, uid, sid)
        else:
            result = await client.send(question, uid, sid)
        v = assess(result)
        logger.info("sql_agent.a2a 完成 trace_id=%s session=%s route=%s queried_db=%s databases=%s "
                    "status=%s(%s) usable=%s",
                    result["trace_id"], sid, result["route"], v.queried_database,
                    result["selected_databases"],
                    v.execution_status, v.status_source, v.usable_as_source)
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
