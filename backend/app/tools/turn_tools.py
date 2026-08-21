"""
规则预取的两路查询：知识库检索和业务数据委派。

/chat 和 /chat/stream 在专家作答之前先把资料查好（见 agents/turn_prep），查不查由
意图和实体的规则决定。查到的东西进上下文；能当出处的同时进 grounding_sources。
这里放的是这两路的门控、执行和格式化。

**为什么从 api/main.py 搬出来。** 原来它们直接读 main 的模块级单例
（`_tool_manager` / `_sql_agent` / `_knowledge_base`），只有 /chat 用得上。评测要复刻
准备阶段就只能自己再写一遍——`scripts/run_eval.py` 里的 `grounding` 就是这么写的，
而且已经和这里漂开了。所以工具改成显式传入（`Prefetch`）：api/main.py 按进程单例
装配一份，别处按自己的工具装配一份，查法只有一套。

每一路拆成两半：

  - 门控（`Prefetch.knowledge_context` / `Prefetch.order_data_context`）：这一轮该不该查；
  - 执行 + 格式化（`knowledge_lookup` / `delegate_order_query`）：查、判定、写成给
    Agent 读的文本。

后一半不含门控：不管这一轮是谁决定要查，查法、信任判定和给 Agent 的措辞都只有这一处。
规则预取用它；工具循环里模型自己发起的调用（文件末尾的 KnowledgeSearchTool /
OrderDataQueryTool）也用它。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.agents.tool_loop import LoopTool, ToolOutcome, TurnContext
from app.core import evidence
from app.core.evidence import assess
from app.core.preflight import outer_tool_timeout_s
from app.tools.knowledge_base import document_excerpt, should_use_knowledge
from app.tools.tool_manager import PASSAGE_CHARS

logger = logging.getLogger(__name__)

# 给知识片段补上所属文档的展示标题（读文档表）。见 api/main.py 的 _label_sources。
LabelSources = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class Prefetch:
    """
    规则预取，绑定到一组工具上。

    `tool_manager` 为 None 时两路都不查；`sql_agent` 为 None 或没配地址时不委派。
    `label_sources` 不传时，知识片段只用它自带的标题。
    """

    tool_manager: Any
    sql_agent: Any = None
    label_sources: LabelSources | None = None

    @property
    def data_enabled(self) -> bool:
        """这个实例接没接业务数据（委派客户端配了地址）。"""
        return bool(self.sql_agent is not None and self.sql_agent.enabled)

    def will_search(self, message: str, intent=None) -> bool:
        """这一轮会不会检索知识库。和 `knowledge_context` 开头的两道门是同一套判断。"""
        return self.tool_manager is not None and should_use_knowledge(message, intent=intent)

    def will_query(self, message: str, intent=None, entities=None) -> bool:
        """这一轮会不会去委派查业务数据。和 `order_data_context` 开头的三道门是同一套判断。"""
        from app.tools.sql_agent_client import should_query_business_data

        if self.tool_manager is None or not self.data_enabled:
            return False
        return bool(should_query_business_data(message, intent=intent, entities=entities))

    async def knowledge_context(
        self,
        message: str,
        intent=None,
        top_k: int = 3,
        fast_path: bool = False,
        trace: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """
        为 /chat 主链路构建 RAG 知识上下文。

        完整链路会复用 ToolManager 的查询改写、并行召回、重排 + 证据判定、
        自纠错、fallback；fast_path=True 时走单次直检，用两次 LLM 往返换检索
        广度——意图已经由规则通道确定的高频问题不需要多角度改写。

        注意这里少掉的只是**改写和重排**这两次 LLM 往返。混合检索（稠密向量 +
        关键词双路 RRF 融合）发生在存储层，不花任何 LLM 调用，所以快通道同样
        享受到它带来的召回提升，零网络往返的承诺不受影响。

        `trace` 传进来的话，这一轮检索的经过会记在里面：走的哪条路、放进上下文的
        是哪几段（`sources`，每段带文档和分片 id）。**只记真的进了上下文的片段**
        ——召回了但被重排挤掉的不算，降级占位的那一条（知识库不可用时的兜底）
        也不算：它不是知识，不能被显示成"回答的依据"。
        """
        info = trace if trace is not None else {}
        info.update(attempted=False, mode="", sources=[], reranked=False,
                    corrected=False, evidence_ok=None, degraded=False)
        if self.tool_manager is None:
            return "", False
        if not should_use_knowledge(message, intent=intent):
            return "", False
        info.update(attempted=True, mode="direct" if fast_path else "rewrite_rerank")
        return await knowledge_lookup(
            self.tool_manager, message, top_k=top_k, fast_path=fast_path,
            trace=info, label_sources=self.label_sources,
        )

    async def order_data_context(
        self,
        message: str,
        intent=None,
        entities: dict | None = None,
        user_id: str = "",
        conv_id: str = "",
        progress_cb=None,
        trace: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """
        为 /chat 主链路构建业务数据上下文：把问题委派给 ai-sql-agent 查真实数据。

        这里只做门控：实例没接业务数据、或这个问题不需要查，就返回空。
        查询、信任判定和格式化见 `delegate_order_query`，返回值的含义也在那里。

        `trace` 传进来的话，这次委派的结局会记在 `outcome` 里，供响应如实报告：
        `not_configured`（这个实例没接业务数据）、`skipped`（这个问题不需要查）、
        `model_unavailable`（本方模型服务不可用，这一轮根本没来委派——由
        `agents/turn_prep.prepare_turn` 直接记下，不经过这个函数）、
        `evidence`（查到了，可作依据）、`unverified`（查到了，没过可信校验）、
        `failed` / `unavailable` / `error`（没查成）、`not_applicable`（下游判断
        不需要查库）、`empty`（查了，没有内容）。和返回值是同一次判定的
        两种说法——一个给 Agent 读，一个给工作台读，不许各判各的。
        """
        from app.tools.sql_agent_client import should_query_business_data

        info = trace if trace is not None else {}
        info.update(enabled=False, attempted=False, outcome="not_configured")
        if self.tool_manager is None or not self.data_enabled:
            return "", False
        info.update(enabled=True, outcome="skipped")
        if not should_query_business_data(message, intent=intent, entities=entities):
            return "", False
        return await delegate_order_query(
            self.tool_manager, message, user_id=user_id, conv_id=conv_id,
            progress_cb=progress_cb, trace=info, currency=self.sql_agent.currency,
        )


async def knowledge_lookup(
    tool_manager: Any,
    query: str,
    *,
    top_k: int = 3,
    fast_path: bool = False,
    trace: dict[str, Any],
    label_sources: LabelSources | None = None,
) -> tuple[str, bool]:
    """
    检索知识库，把前 `top_k` 段写成给 Agent 读的上下文。不做门控。

    返回 `(上下文文本, 用没用到)`；查不到、出错都返回空，不中断这一轮。
    `trace` 的字段见 `Prefetch.knowledge_context`。
    """
    info = trace
    try:
        if fast_path:
            result = await tool_manager.call(
                "knowledge_search", {"query": query, "top_k": top_k}
            )
        else:
            result = await tool_manager.search_with_rewrite(
                "knowledge_search", query, top_k=top_k
            )
        if not result.success or not isinstance(result.data, list) or not result.data:
            return "", False
        info.update(reranked=result.reranked, corrected=result.corrected,
                    evidence_ok=result.evidence_ok)

        parts = ["[知识库检索结果]"]
        sources: list[dict[str, Any]] = []
        used = False
        for i, item in enumerate(result.data[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "未命名文档"))
            content = str(item.get("content", "")).strip()
            score = item.get("score", "")
            if not content:
                continue
            used = True
            # 只走了关键词一路的结果没有向量分（score 是 None），不给模型看一行"相关度: None"
            relevance = f"   相关度: {score}\n" if isinstance(score, (int, float)) else ""
            parts.append(f"{i}. 标题: {title}\n{relevance}   内容: {content[:PASSAGE_CHARS]}")
            if item.get("fallback"):
                info["degraded"] = True
                continue
            sources.append({
                "doc_id": str(item.get("doc_id") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
                "title": title,
                "passage": item.get("chunk", 0),
                "score": score if isinstance(score, (int, float)) else None,
                "retrieval": str(item.get("retrieval") or ""),
                # 给人看的摘录去掉开头的标题行和 Markdown 记号；喂给模型的那份（上面的
                # parts）不动。截断处带省略号——硬切在词中间（"…Curbsi"）读起来像
                # 段落本身坏了，而被切掉的往往正是回答依据的那一句
                "excerpt": document_excerpt(content, 240),
            })

        if not used:
            return "", False
        info["sources"] = await label_sources(sources) if label_sources else sources
        parts.append("请优先依据以上知识库内容回答；如果知识库内容不足，再结合通用客服能力说明。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")
        return "", False


def currency_note(data: dict, configured: str = "") -> str:
    """
    金额币种的说明句。**币种的来源必须在句子里可见。**

    契约 §4.6.5 把这一条单独立了一节，理由是它已经出过一次业务事故：下游只
    返回数字不返回币种，中文回答就默认写成「元」，接美国厂的库时报给 builder
    的金额带了错误的币种。当时的补救是在调用方配一个全局常量
    `SQL_AGENT_CURRENCY` 顶上——**那是个跨仓库配置在替契约打补丁**：
    它是**部署**的属性不是**数据**的属性，一个部署里只要有多币种的库，
    它必然给出错误答案，而且报出去的金额看起来完全正常。

    契约 v1.8 把这一条改成**按来源三分**，来源判定在
    `core.evidence.resolve_currency()`（`currency_source`，与 `status_source`
    同构）；这里只负责把那个来源**说进句子里**：

    1. `contract` —— 数据自己声明了 `currency` → 按数据说，可以当事实；
    2. `deployment_config` —— 部署配了全局币种 → 可以用，但**明写它来自部署
       配置**、不是本次查询返回的字段；
    3. `unknown` —— 两者都没有 → 只写数字，并**主动禁止**语言的默认值。

    第三档是一条**指令**不是一条禁令。契约 v1.6/v1.7 那条"不得补币种"是禁令，
    而自然语言有自己的默认值：中文回答不说币种就会写成「元」。
    "不说"会被默认值填上，所以规则必须写成"说什么"。

    `configured` 是委派客户端上配置的币种（`SQL_AGENT_CURRENCY`），没配为空串。
    """
    currency, source = evidence.resolve_currency(data, configured)
    if source == evidence.CURRENCY_CONTRACT:
        return (
            f"以上金额的币种是 {currency}（本次查询随数据一同返回）。"
            f"提到金额时按 {currency} 表述，不要换算，也不要写成其它币种。"
        )
    if source == evidence.CURRENCY_DEPLOYMENT_CONFIG:
        return (
            f"以上金额按 {currency} 表述，不要换算，也不要写成其它币种。"
            f"但请注意：{currency} 来自本部署的配置（SQL_AGENT_CURRENCY），"
            "**不是**本次查询返回的字段——业务系统目前不返回币种。"
            "因此不得把币种说成系统核实过的结果；客户对币种有疑问时请转人工确认。"
        )
    return (
        "业务系统不返回币种，本部署也没有声明。提到金额时**只写数字，不要补"
        "任何币种符号或单位**（不要默认写成「元」），并说明币种需要人工确认。"
    )


async def delegate_order_query(
    tool_manager: Any,
    query: str,
    *,
    user_id: str = "",
    conv_id: str = "",
    progress_cb=None,
    trace: dict[str, Any],
    currency: str = "",
) -> tuple[str, bool]:
    """
    把问题委派给 ai-sql-agent，判定结果能不能当出处，写成给 Agent 读的上下文。不做门控。

    返回 `(上下文文本, 是否可作为依据)`。**第二个值不是"用没用到"，是"能不能
    当出处"**——调用方据此决定要不要放进 `grounding_sources`。两种非空返回：

    - **真数据** → `(数据块, True)`，进上下文也进 grounding sources；
    - **降级提示** → `(提示块, False)`，只进上下文**不进** grounding sources。
      它是写给 Agent 的指令而不是证据，混进 sources 会让答案策略拿它去给
      价格/日期"找出处"，等于用一句"查询失败了"来批准一个凭空的数字。

    任何一环不成立都返回空——这是增强项，拿不到数据时链路照常走知识库 + SOP，
    不能因此中断。

    **"拿到了响应但不该当依据"的情况，都要挡在这里。** 判定不在这个函数里
    自己写，统一走 `core.evidence.assess()`——三个承载者共用同一套口径
    （契约 §4.4.1），否则回滚（§6.2）会变成静默的信任口径切换。分四类：

    1. `unavailable` —— 下游挂了或熔断，fallback 给的占位结果。返回降级提示；
    2. `not_applicable` —— ai-sql-agent 自己判断这问题不需要查库，走了对话
       路径。那个 answer 是模型说的话，**不是数据**，把它当依据注入等于给
       一段没有出处的文本盖了"实时数据"的章。**连上下文都不进**；
    3. `failed` —— 说查了库，但一步都没成。**这一类以前看不见**：
       `queried_database` 的服务端定义只有一行 `pipeline_result is not None`，
       SQL 全跑挂了它也是 `true`，终态照发 `completed`。于是模型编的话会被
       当成"业务系统查到的真实数据"讲给客户。现在按 §4.6.1 的判据从
       `steps[]` 判出来，返回一段明写"没查成"的提示，**不作为出处**；
    4. `has_risk` / `degraded` / 缺字段 / 字符串布尔 —— 数据**进上下文**
       （丢掉比现状更糟，§4.6.1），但**不进 grounding_sources**，
       并带上明确的不确定表述约束。以前这两个标志只追加一句提示、
       函数照样返回 True，等于带风险的数据和已核实的金额共享同一个可信标志。

    另外，空 answer 直接返回空：查了但没有可讲的内容。

    **关于把它放进 grounding_sources（安全相关，别随手改）。** AnswerPolicy 会
    拦截"无依据的价格/时效承诺"，判据是承诺里的数字能不能在本轮 grounding
    sources 里找到出处。订单交期、账款金额恰好就是这两类数字——如果查回来的
    真实数据不进 sources，客服说"您的柜体 9 月 15 日出厂"会被当成无依据承诺
    替换成安全话术，整条委派看起来就像没生效。所以这段文本必须进 sources；
    反过来，上面第 2 条那种"不是数据的文本"绝不能进。

    `trace` 的 `outcome` 见 `Prefetch.order_data_context`；`currency` 是委派客户端上
    配置的币种，见 `currency_note`。
    """
    info = trace
    info.update(attempted=True, outcome="error")

    try:
        result = await tool_manager.call(
            "order_data_query",
            {"query": query, "user_id": user_id, "session_id": conv_id},
            # 走 A2A 流式时，下游的阶段进度经这个回调进 /chat/stream 的 progress 事件
            context={"progress_cb": progress_cb} if progress_cb else None,
        )
        data = result.data if isinstance(result.data, dict) else {}

        # 委派失败（超时、熔断、下游异常）。**不静默**——实测里最危险的一种
        # 形态就是这个：下游其实查完了（HTTP 200），只是调用方先放弃，客户
        # 拿到一段听起来完全合理、实则没有任何系统依据的回答，客户和坐席
        # 都分辨不出来。这里把失败明写进上下文，要求 Agent 说出来。
        if data.get("unavailable") or (not result.success and not data):
            reason = str(data.get("error") or result.error or "未知原因")
            info.update(outcome="unavailable", reason=reason)
            return "\n".join([
                "[业务系统查询未完成]",
                f"本轮尝试查询订单/生产/库存/账款系统，但没有拿到结果（{reason}）。",
                "回答时必须做到两点：",
                "1. 不要给出任何具体的订单状态、日期、金额、库存数字——这些本轮没有系统依据；",
                "2. 明确告诉客户这次没能查到系统记录，建议由人工跟进核实，不要让客户以为"
                "你说的内容已经过系统核对。",
            ]), False

        # 信任判定（契约 §4.6）。**唯一的一处**——三个承载者
        # （stream / send / 直连 /query）走的是同一个 assess()，
        # 因为回滚会让流量在它们之间切换（契约 §6.2 与 §4.4.1），
        # 判定要是分了叉，回滚就成了静默的信任口径切换。
        verdict = assess(data)

        # 下游自己判断这问题不需要查库（走了对话路径）。不是故障，不提示。
        # 那段 answer 是模型说的话不是数据，连上下文都不该进。
        if verdict.execution_status == evidence.NOT_APPLICABLE:
            info.update(outcome="not_applicable")
            return "", False

        trace_id = str(data.get("trace_id", "") or "")
        info.update(
            trace_id=trace_id,
            databases=[str(d) for d in data.get("selected_databases") or []],
            execution_status=verdict.execution_status,
        )

        # 查库路径但一步都没成。这里**必须**和"查到了但没有记录"分开：
        # `queried_database` 两种情况下都是 true（服务端定义只有一行
        # `pipeline_result is not None`），只看它会把"查挂了"讲成真实数据。
        if verdict.execution_status == evidence.FAILED:
            logger.warning(
                "业务数据查询未成功，不作为出处 trace_id=%s status=%s(%s) 原因=%s",
                trace_id, verdict.execution_status, verdict.status_source,
                "；".join(verdict.reasons) or "-",
            )
            info.update(outcome="failed", reasons=list(verdict.reasons))
            return "\n".join([
                "[业务系统查询未成功]",
                f"本轮查询业务系统的每一个步骤都没有成功执行（trace_id: {trace_id or '未知'}）。",
                "回答时必须做到两点：",
                "1. 不要给出任何具体的订单状态、日期、金额、库存数字——这些本轮没有系统依据；",
                "2. 明确告诉客户这次没能查到系统记录，建议由人工跟进核实，不要让客户以为"
                "你说的内容已经过系统核对。",
            ]), False

        answer = str(data.get("answer", "")).strip()
        if not answer:
            info.update(outcome="empty")
            return "", False

        # ai-sql-agent 的 answer 里嵌的是查询结果的 JSON 原文，行数多的时候
        # 能到几万字符（它那边 max_rows 默认 10000）。整段塞进 prompt 会把
        # 上下文顶爆，也会把知识库那部分挤掉，所以这里截断。
        # 截断阈值调大之前先想清楚：真正该做的是让下游少返点行，不是这边多收点。
        limit = int(os.getenv("SQL_AGENT_MAX_CONTEXT_CHARS", "2000"))
        if len(answer) > limit:
            answer = answer[:limit] + f"\n…（结果过长已截断，完整结果见 trace_id {trace_id}）"

        databases = ", ".join(str(d) for d in data.get("selected_databases") or []) or "未标明"
        # 标题就把可信度写在第一行：可作出处的那段叫「实时数据」，不可作出处的
        # 那段绝不能也叫这个名字——Agent 读到的第一行决定它怎么措辞。
        header = "[业务系统实时数据]" if verdict.usable_as_source else "[业务系统数据（未通过可信校验，仅供参考）]"
        parts = [
            header,
            f"来源: ai-sql-agent（命中库: {databases}；trace_id: {trace_id}；"
            f"执行状态: {verdict.execution_status}）",
            answer,
        ]
        if verdict.has_risk:
            parts.append(
                "注意: 这条查询结果带有异常提示（空结果 / 基数异常 / 语义不一致），"
                "SQL 跑通了但答的可能不是问的那件事。**不得**把其中的金额、日期、"
                "订单状态说成已核实的结论，只能说明系统里查到了这些内容、需要人工核对。"
            )
        if verdict.degraded:
            parts.append(
                f"注意: 数据服务处于降级状态（{data.get('degrade_reason', '') or '原因未说明'}），"
                "结果可能来自历史查询重放，可能已经过期。**不得**表述为当前状态，"
                "必须请客户以正式单据为准。"
            )
        parts.extend(f"注意: {c}" for c in verdict.caveats)
        parts.append(currency_note(data, currency))
        parts.append(
            "以上是业务系统查到的真实数据，回答订单/交期/库存/账款问题时优先依据它。"
            if verdict.usable_as_source else
            "以上内容**没有通过可信校验**，不能当作已核实的系统依据。"
            "涉及具体日期、金额、库存数字时必须明说这一点，并建议人工核实。"
        )
        if not verdict.usable_as_source:
            logger.warning(
                "业务数据未通过可信校验，不进 grounding_sources trace_id=%s status=%s(%s) 原因=%s",
                trace_id, verdict.execution_status, verdict.status_source,
                "；".join(verdict.reasons) or "-",
            )
        info.update(
            outcome="evidence" if verdict.usable_as_source else "unverified",
            has_risk=verdict.has_risk,
            degraded=verdict.degraded,
            reasons=list(verdict.reasons),
        )
        return "\n".join(p for p in parts if p), verdict.usable_as_source
    except Exception as ex:
        logger.warning(f"构建业务数据上下文失败: {ex}")
        info.update(outcome="error", reason=str(ex) or type(ex).__name__)
        return "", False


# ── 工具循环里模型能调的两个工具 ──────────────────────────────────────────────
#
# 查法、信任判定和给模型的措辞都复用上面那两半，和规则预取一字不差：模型拿到的
# 是和背景信息里同样格式的资料，答案守卫拿到的是同样口径的出处。
#
# 模型能填的只有查询词。身份（user_id / conv_id）从 TurnContext 取——那是服务端从
# 请求里推导出来的可信身份，不在模型看得到的参数里，提示注入改不了。

class KnowledgeSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200,
                       description="完整的检索句：补上前文提到的产品、订单或问题，不要只是原样重复客户这句话")
    top_k: int = Field(default=3, ge=1, le=5, description="要几段，默认 3")


class OrderDataQueryArgs(BaseModel):
    query: str = Field(min_length=1, max_length=300,
                       description="一句完整的查询，写上能定位单据的信息：订单号、柜号或日期")


def knowledge_outcome(text: str, used: bool, trace: dict[str, Any]) -> ToolOutcome:
    """一次知识库检索 → 交给模型的结果。检索到的片段和规则预取一样算出处。"""
    if used and text:
        return ToolOutcome(content=text, evidence=text, trace=trace, outcome="found")
    return ToolOutcome(content="知识库里没有找到相关内容。", trace=trace, outcome="empty")


_ORDER_FAILED = frozenset({"unavailable", "failed", "error"})
_ORDER_NO_TEXT = {
    "not_applicable": "业务系统判断这个问题不需要查库，没有返回数据。",
    "empty": "业务系统查了，但没有可以讲的内容。",
}


def order_outcome(text: str, is_evidence: bool, trace: dict[str, Any]) -> ToolOutcome:
    """
    一次业务数据委派 → 交给模型的结果。

    能不能当出处完全照 `delegate_order_query` 的判定；没查成的那几种标成 is_error，
    内容里照旧是那两段"不要给具体数字"的指令。
    """
    outcome = str(trace.get("outcome") or "")
    if text:
        content = text
    elif outcome == "error":
        content = (f"业务系统查询出错（{trace.get('reason') or '未知原因'}）。"
                   "不要给出任何具体的订单状态、日期、金额、库存数字。")
    else:
        content = _ORDER_NO_TEXT.get(outcome, "业务系统没有返回数据。")
    return ToolOutcome(content=content, is_error=outcome in _ORDER_FAILED,
                       evidence=text if is_evidence else "", trace=trace, outcome=outcome)


class KnowledgeSearchTool(LoopTool):
    name = "knowledge_search"
    description = (
        "检索知识库：产品规格、安装调整、售后流程和政策。背景信息里已经有按客户这句话检索到的结果，"
        "只有那些不够回答时才用，换一个更完整的检索句。"
    )
    params = KnowledgeSearchArgs

    def __init__(self, tool_manager: Any, label_sources: LabelSources | None = None):
        self.tool_manager = tool_manager
        self.label_sources = label_sources

    async def run(self, args: KnowledgeSearchArgs, turn: TurnContext) -> ToolOutcome:
        # 直接检索，不再跑 LLM 改写和重排：查询词是模型写的（相当于改写），结果也由它
        # 自己读（相当于判断证据够不够）。再跑一遍，每次检索要多两次 LLM 调用。
        trace: dict[str, Any] = {"attempted": True, "mode": "model", "sources": [], "reranked": False,
                                 "corrected": False, "evidence_ok": None, "degraded": False}
        text, used = await knowledge_lookup(self.tool_manager, args.query, top_k=args.top_k,
                                            fast_path=True, trace=trace, label_sources=self.label_sources)
        return knowledge_outcome(text, used, trace)


class OrderDataQueryTool(LoopTool):
    name = "order_data_query"
    description = (
        "查业务系统里的订单、生产、送货、补件、账款记录（只读）。背景信息里没有需要的系统数据时才用。"
        "报价不走这里。"
    )
    params = OrderDataQueryArgs
    slow = True

    def __init__(self, tool_manager: Any, sql_agent: Any):
        self.tool_manager = tool_manager
        self.sql_agent = sql_agent
        self.timeout_s = outer_tool_timeout_s()

    def refuse(self, turn: TurnContext) -> str:
        # 和 sql_agent_client.DEFAULT_DATA_INTENTS 把报价排除在外是同一个理由：报价是对外
        # 承诺，出口要走正式报价单，不该由一句自然语言查询直接给数。
        if (turn.intent or "") == "quote_pricing":
            return ("报价不通过系统查询：报价是对外承诺，必须由销售出具正式报价单。"
                    "不要给出任何具体金额。")
        return ""

    def skip(self, turn: TurnContext) -> str:
        # 规则预取这一轮刚委派过，下游超时、熔断或出错（outcome=unavailable）：再委派多半
        # 还要等满一次超时（SQL_AGENT_TIMEOUT_S=90 时是 92 秒），一轮会拖到三分多钟，结果
        # 还是查不到。failed 不算：那是下游答了、只是没查成，换个查询词可能就对了。
        prior = next((e for e in turn.trajectory if e.get("actor") == "rule" and e.get("tool") == self.name
                      and e.get("outcome") == "unavailable"), None)
        if prior is None:
            return ""
        reason = str((prior.get("trace") or {}).get("reason") or "没有响应")
        return (f"业务系统这一轮已经没有响应（{reason}），这次不再查询。根据已有资料作答："
                "不要给出任何具体的订单状态、日期、金额、库存数字，告诉客户这次没能查到系统记录，"
                "建议由人工跟进核实。")

    async def run(self, args: OrderDataQueryArgs, turn: TurnContext) -> ToolOutcome:
        trace: dict[str, Any] = {"enabled": True}
        text, is_evidence = await delegate_order_query(
            self.tool_manager, args.query, user_id=turn.user_id, conv_id=turn.conv_id,
            progress_cb=turn.progress_cb, trace=trace, currency=self.sql_agent.currency,
        )
        return order_outcome(text, is_evidence, trace)


def loop_tools(prefetch: Prefetch) -> dict[str, LoopTool]:
    """这一份工具上模型能调的工具。没接业务数据的实例（比如客户实例）没有 order_data_query。"""
    if prefetch.tool_manager is None:
        return {}
    tools: dict[str, LoopTool] = {
        "knowledge_search": KnowledgeSearchTool(prefetch.tool_manager, prefetch.label_sources),
    }
    if prefetch.data_enabled:
        tools["order_data_query"] = OrderDataQueryTool(prefetch.tool_manager, prefetch.sql_agent)
    return tools


def _rule_record(tool: str, message: str, outcome: ToolOutcome) -> dict[str, Any]:
    return {
        "step": 0, "call_id": "", "actor": "rule", "agent": None, "tool": tool,
        "args": {"query": message}, "outcome": outcome.outcome, "is_error": outcome.is_error,
        "evidence": bool(outcome.evidence), "dedup": False, "latency_ms": None, "trace": outcome.trace,
    }


def seed_from_prefetch(
    turn: TurnContext,
    message: str,
    *,
    knowledge: tuple[str, bool, dict[str, Any]],
    order: tuple[str, bool, dict[str, Any]],
) -> None:
    """
    把规则预取记进这一轮的轨迹和去重表。

    轨迹因此从"规则查了什么"开始；模型再拿客户这句原话查同一样东西时直接复用，
    不再花一次——委派一次要十几秒。只记真的查了的那一路。
    """
    text, used, trace = knowledge
    if trace.get("attempted"):
        outcome = knowledge_outcome(text, used, trace)
        turn.trajectory.append(_rule_record("knowledge_search", message, outcome))
        if "knowledge_search" in turn.tools:
            args = KnowledgeSearchArgs.model_construct(query=message, top_k=3)
            turn.remember(turn.tools["knowledge_search"].memo_key(args), outcome)

    text, is_evidence, trace = order
    if trace.get("attempted"):
        outcome = order_outcome(text, is_evidence, trace)
        turn.trajectory.append(_rule_record("order_data_query", message, outcome))
        if "order_data_query" in turn.tools:
            args = OrderDataQueryArgs.model_construct(query=message)
            turn.remember(turn.tools["order_data_query"].memo_key(args), outcome)
