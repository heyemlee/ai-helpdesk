"""
亮点：工具调用治理框架

**为什么这个包不叫 mcp。** 早期它叫 `backend/mcp/`、类叫 `MCPToolManager`，但它
和 Model Context Protocol 没有关系——MCP 是一套跨进程的 JSON-RPC 协议（无状态核心、
Streamable HTTP 传输、OAuth 授权），而这里是**进程内**的工具注册表加治理逻辑，
不走任何协议。顶着那个名字有两个实际代价：读代码的人以为能拿 MCP 客户端接进来，
以及 `pip install mcp`（官方 SDK 的包名）之后本地包会把它整个遮蔽掉。
真要对外提供 MCP server 是另一件事，等工具需要被别的客户端复用时再做。

核心问题：工具调用出错（检索不全、召回不好）怎么优化？

本模块的答案：
  1. 查询改写（Query Rewriting）—— 用 LLM 把用户原始问题扩写成多个角度的子查询，
     再合并去重，解决"召回不全"问题。
  2. 结果重排（Reranking）—— 对召回结果按相关性重新排序，解决"召回不好/排序差"
     问题。优先走 cross-encoder（判别式模型，几十毫秒），未配置时回落 LLM listwise。
  3. 检索自纠错（Self-Correction）—— 重排完不是直接交付，先判定证据是否足以
     回答问题；不足则围绕缺口补检一轮。**硬性上限一轮**，见 search_with_rewrite。
  4. 熔断器（Circuit Breaker）—— 连续失败超阈值时自动断开，防止雪崩。
  5. 结果缓存（TTL Cache）—— 相同参数直接返回缓存，减少重复调用。
  6. 降级策略（Fallback）—— 工具不可用时返回有意义的降级结果。

关于第 3 点的成本：朴素做法是重排之后再调一次 LLM 问"证据够不够"，
那是**每次检索多一次往返**。这里不这么做——LLM 重排本来就要把 query 和
全部候选读一遍，判定证据充分性所需的信息一字不差就是这一份，所以充分性
判定被合并进重排的同一次调用、同一份 JSON 里返回，额外往返数为零。
只有在改用 cross-encoder 重排（没有可搭车的生成调用）时才退回分数阈值
判定——同样零往返，只是判据从"模型说够不够"变成"最高相似度够不够"。
"""
import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.core.llm_guard import log_model_failure
from app.core.llm_utils import (
    DEFAULT_ANTHROPIC_MODEL,
    STRUCTURED_MAX_TOKENS,
    create_llm_client,
    structured_parse,
)

logger = logging.getLogger(__name__)

# 没有 LLM 判定可用时（cross-encoder 重排路径），认为"证据充分"的最低相似度。
# 低于它才触发补检索。定成 0.5 是因为余弦相似度在 0.5 以下时，检索命中的
# 通常只是同领域的泛泛内容而不是能回答该问题的具体条款。
EVIDENCE_MIN_SCORE = float(os.getenv("RAG_EVIDENCE_MIN_SCORE", "0.5"))

# 一条知识片段给模型看多少字，LLM 重排和最后交给 Agent 的上下文（api/main.py 的
# _build_knowledge_context）用同一个数。重排那次调用顺带判"这些内容够不够回答"，
# 它读到的就得是之后真正拿去回答的那一段——读得比 Agent 少，条款的后半句
# （"但已开料的不退"）它看不见，会把够用的证据判成不足、白补一轮检索；
# 读得比 Agent 多，又会拿 Agent 看不到的内容判"充分"。
# 600 盖得住一整个分片（切片上限 500 字，见 knowledge_base.CHUNK_SIZE）。
PASSAGE_CHARS = 600


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "closed"     # 正常
    OPEN      = "open"       # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 探测恢复


@dataclass
class ToolResult:
    success:        bool
    data:           Any
    tool_name:      str
    error:          str | None = None
    cached:         bool = False
    latency_ms:     float = 0.0
    reranked:       bool = False   # 是否经过重排
    corrected:      bool = False   # 是否触发过检索自纠错（补检一轮）
    evidence_ok:    bool | None = None  # 证据充分性判定；None = 未判定


class QueryRewritePayload(BaseModel):
    """多角度改写的返回形状。"""

    queries: list[str]


class RewrittenQueries(list):
    """
    `rewrite_query` 的返回值：还是一个子查询列表，多带一个 `failed`。

    改写那次模型调用失败时，返回的是"只有原始查询"的降级列表——和模型正常返回
    但只给了一条，在列表上看不出区别。`search_with_rewrite` 要分得清这两件事
    （调用失败不能再补检一轮），所以把它标在返回值上。
    """

    def __init__(self, queries=(), failed: bool = False):
        super().__init__(queries)
        self.failed = failed


class RerankPayload(BaseModel):
    """
    重排 + 证据充分性判定的返回形状，由结构化输出在服务端约束。

    这个 schema 替掉了原来的 `_parse_rerank_payload`：那个函数要兼容"模型只返回
    了一个裸数组"的情况，此时充分性判定取 None（未判定）。schema 生效之后
    sufficient 是必填布尔，那条兼容分支不再可能发生，跟着一起删了。

    注意 RerankOutcome.sufficient 仍然保留 None——它现在只表示另外两种情况：
    走的是 cross-encoder 路径（压根没问模型），或候选太少没触发重排。
    """

    order: list[int]
    sufficient: bool
    missing: str


@dataclass
class RerankOutcome:
    """
    重排结果 + 顺带得到的证据充分性判定。

    sufficient 为 None 表示这次重排无法判定（cross-encoder 路径，或候选数
    太少压根没调用模型）——调用方据此回落到分数阈值判定，而不是把 None
    当成 False 白白触发一轮补检索。
    """
    items:      list[Any]
    reranked:   bool
    sufficient: bool | None = None
    missing:    str = ""           # 模型指出的证据缺口，用于定向补检索
    # 重排那次模型调用本身失败了（超时、凭证被拒……）。和 sufficient=None 的另外
    # 两种情况不同：那两种是"没去问模型"，可以退回分数阈值；这一种是"问了没问成"，
    # 不能当成模型判了"证据不足"——见 search_with_rewrite。
    failed:     bool = False


@dataclass
class ToolStats:
    """工具运行时统计，供 Monitor 读取。"""
    total:              int = 0
    success:            int = 0
    failed:             int = 0
    total_latency_ms:   float = 0.0
    consecutive_fails:  int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0


# ── 熔断器 ────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    三态熔断器：CLOSED → OPEN → HALF_OPEN → CLOSED

    连续失败 failure_threshold 次后打开；
    打开 recovery_s 秒后进入 HALF_OPEN 探测；
    探测成功则关闭，失败则重新打开。
    """

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0):
        self.threshold   = failure_threshold
        self.recovery_s  = recovery_s
        self.state       = CircuitState.CLOSED
        self.fail_count  = 0
        self.opened_at:  float | None = None

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_s:  # type: ignore
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN：放行一次探测

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.threshold:
            self.state     = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(f"熔断器打开（连续失败 {self.fail_count} 次）")


# ── 工具定义 ──────────────────────────────────────────────────────────────────

@dataclass
class Tool:
    name:        str
    description: str
    handler:     Callable                    # async (params, context) -> Any
    schema:      dict[str, Any]              # JSON Schema
    cache_ttl:   float = 0.0                 # 0 = 不缓存
    timeout_s:   float = 30.0
    supports_rerank: bool = False            # 是否支持结果重排
    fallback:    Callable | None = None    # sync/async (params, context, error) -> Any

    # 运行时状态（不参与构造）
    stats:   ToolStats    = field(default_factory=ToolStats, init=False)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker, init=False)


# ── 工具管理器 ────────────────────────────────────────────────────────────

class ToolManager:
    """
    工具调用治理框架。

    核心优化链路（针对检索类工具）：
      用户查询 → 查询改写（多角度子查询）→ 并行召回 → 结果重排
              → 证据充分性判定 →（不足则补检一轮）→ 返回 Top-K
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        shared_state=None,
        reranker=None,
    ):
        self._client = create_llm_client(api_key, base_url)
        self._model  = model
        self._tools: dict[str, Tool] = {}
        self._cache: dict[str, tuple] = {}   # key → (result, expire_at, reranked)
        # 可选的 cross-encoder 重排器（core/reranker.py）。为 None 或未配置远端时
        # 全部走 LLM listwise 重排，行为与加它之前完全一致。
        self._reranker = reranker
        # 多实例部署时传入 SharedState，熔断状态经 Redis 共享：实例 A 熔断后
        # 实例 B 对同一工具的调用同样被拒绝。未传入时用进程内熔断器（单实例语义）。
        self._shared_state = shared_state
        self._dist_breakers: dict[str, Any] = {}

    # ── 注册 / 注销 ───────────────────────────────────────────────────────────

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        if self._shared_state is not None:
            from app.core.shared_state import DistributedCircuitBreaker
            self._dist_breakers[tool.name] = DistributedCircuitBreaker(
                self._shared_state,
                tool.name,
                failure_threshold=tool.breaker.threshold,
                recovery_s=tool.breaker.recovery_s,
            )
        logger.info(f"注册工具: {tool.name}")

    # ── 熔断（进程内 / 分布式统一入口）────────────────────────────────────────

    async def _breaker_allow(self, tool: Tool) -> bool:
        dist = self._dist_breakers.get(tool.name)
        if dist is not None:
            return await dist.allow()
        return tool.breaker.allow()

    async def _breaker_success(self, tool: Tool) -> None:
        dist = self._dist_breakers.get(tool.name)
        if dist is not None:
            await dist.record_success()
        tool.breaker.record_success()  # 本地始终记录，get_stats 不依赖 Redis

    async def _breaker_failure(self, tool: Tool) -> None:
        dist = self._dist_breakers.get(tool.name)
        if dist is not None:
            await dist.record_failure()
        tool.breaker.record_failure()

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # ── 核心调用 ──────────────────────────────────────────────────────────────

    async def call(
        self,
        name: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
        rerank_top_k: int = 0,          # >0 时对结果重排，取 Top-K
    ) -> ToolResult:
        """
        调用工具，完整执行链：
          缓存检查 → 熔断检查 → 参数校验 → 执行（含超时）→ 可选重排 → 缓存写入
        """
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, data=None, tool_name=name, error=f"工具不存在: {name}")

        cache_rerank_top_k = rerank_top_k if rerank_top_k > 0 and tool.supports_rerank else 0

        # 缓存命中
        if use_cache and tool.cache_ttl > 0:
            cached = self._get_cache(name, params, cache_rerank_top_k)
            if cached is not None:
                cached_data, cached_reranked = cached
                tool.stats.total += 1
                tool.stats.success += 1
                return ToolResult(
                    success=True,
                    data=cached_data,
                    tool_name=name,
                    cached=True,
                    reranked=cached_reranked,
                )

        # 熔断检查
        if not await self._breaker_allow(tool):
            error = f"工具熔断中: {name}，请稍后重试"
            return await self._fallback_result(tool, params, context, error)

        t0 = time.monotonic()
        # total 在**结束时**和 success/failed 一起记，不在开始时先加。
        # 开始就加会让在途请求把成功率稀释成 0：Monitor 每 10s 采一次，
        # 而 order_data_query 这类委派工具一次要跑十几秒，几乎每次采样都落在
        # 在途窗口里，一个从没失败过的工具会被报成 0.000 成功率。
        # Agent 侧的同一个毛病已经这么修过，工具侧当时漏了。
        try:
            # 参数校验（根据 JSON Schema 的 required 和 properties.type）
            self._validate_params(tool, params)

            data = await asyncio.wait_for(self._run_handler(tool, params, context), timeout=tool.timeout_s)
            latency = (time.monotonic() - t0) * 1000

            tool.stats.total += 1
            tool.stats.success += 1
            tool.stats.consecutive_fails = 0
            tool.stats.total_latency_ms += latency
            await self._breaker_success(tool)

            # 重排（针对返回列表的检索工具）
            reranked = False
            if rerank_top_k > 0 and tool.supports_rerank and isinstance(data, list):
                outcome = await self._rerank(params.get("query", ""), data, rerank_top_k)
                data, reranked = outcome.items, outcome.reranked

            # 写缓存：缓存最终返回结果，避免下次命中未重排的原始结果。
            if tool.cache_ttl > 0:
                self._set_cache(name, params, data, tool.cache_ttl, cache_rerank_top_k, reranked)

            return ToolResult(success=True, data=data, tool_name=name,
                              latency_ms=latency, reranked=reranked)

        except TimeoutError:
            tool.stats.total += 1
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            await self._breaker_failure(tool)
            logger.error(f"工具超时: {name} ({tool.timeout_s}s)")
            return await self._fallback_result(tool, params, context, "执行超时")

        except Exception as ex:
            tool.stats.total += 1
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            await self._breaker_failure(tool)
            # 有些异常的 str() 是空串（httpx.ReadTimeout 就是），只打 {ex} 会得到
            # 「工具异常: order_data_query — 」这种没有任何线索的日志。带上类型名。
            detail = str(ex) or type(ex).__name__
            logger.error(f"工具异常: {name} — {type(ex).__name__}: {detail}")
            return await self._fallback_result(tool, params, context, detail)

    async def _fallback_result(
        self,
        tool: Tool,
        params: dict[str, Any],
        context: dict[str, Any] | None,
        error: str,
    ) -> ToolResult:
        """工具不可用时返回降级结果，而不是把空错误直接暴露给调用方。"""
        if tool.fallback is None:
            return ToolResult(success=False, data=None, tool_name=tool.name, error=error)
        try:
            data = tool.fallback(params, context, error)
            if asyncio.iscoroutine(data):
                data = await data
            return ToolResult(
                success=True,
                data=data,
                tool_name=tool.name,
                error=error,
            )
        except Exception as ex:
            logger.error(f"工具降级失败: {tool.name} — {ex}")
            return ToolResult(success=False, data=None, tool_name=tool.name, error=f"{error}; fallback失败: {ex}")

    async def _run_handler(
        self,
        tool: Tool,
        params: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> Any:
        """
        执行工具 handler。

        优先支持 async handler；如果历史工具仍是同步函数，则放入线程池执行，
        避免阻塞事件循环。
        """
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(params, context)
        result = await asyncio.to_thread(tool.handler, params, context)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── 查询改写（解决召回不全）────────────────────────────────────────────────

    async def rewrite_query(self, query: str, n: int = 3, hint: str = "") -> list[str]:
        """
        用 LLM 将原始查询改写为 n 个不同角度的子查询。

        目的：单一查询往往只能召回某一角度的文档，
        多角度子查询并行检索后合并，显著提升召回率。

        示例：
          原始: "补件流程"
          改写: ["缺件漏发怎么登记", "补件需要提供什么材料", "补件多久能到"]

        hint 是自纠错回合传进来的证据缺口描述。带 hint 的改写必须**换角度**
        而不是把上一轮的子查询换个说法——上一轮已经把那些角度检索过了，
        重复的子查询会命中缓存、返回同一批候选，补检索这一轮就白花了。
        """
        gap = f"\n上一轮检索到的内容不足以回答问题，缺少：{hint}\n请围绕这个缺口改写，不要重复上一轮的角度。" if hint else ""
        prompt = f"""将以下用户查询改写为 {n} 个不同角度的搜索子查询，用于检索知识库。
要求：每个子查询角度不同，覆盖原始问题的不同方面。
原始查询: "{query}"{gap}"""
        prompt = self._clean_text(prompt)
        try:
            payload = await structured_parse(
                self._client,
                output_format=QueryRewritePayload,
                model=self._model, max_tokens=STRUCTURED_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            queries = [q for q in (s.strip() for s in payload.queries) if q]
            # 原始查询也保留，去重。补检索回合不再重复原始查询——它的结果
            # 已经在上一轮的候选池里了。
            base = [] if hint else [query]
            return RewrittenQueries(dict.fromkeys(base + queries))
        except Exception as ex:
            log_model_failure(logger, "查询改写失败，使用原始查询", ex)
            return RewrittenQueries([query], failed=True)

    async def search_with_rewrite(
        self,
        tool_name: str,
        query: str,
        top_k: int = 5,
        context: dict[str, Any] | None = None,
        *,
        self_correct: bool = True,
    ) -> ToolResult:
        """
        完整的检索优化链路：
          查询改写 → 并行召回 → 去重 → 重排 + 证据判定 →（不足则补检一轮）→ Top-K

        这是解决"检索不全、召回不好"的完整方案。self_correct 打开时链路从
        单向管道变成有反馈的回路：模型看过候选后如果认为证据不足以回答问题，
        围绕缺口再检索一轮。

        **为什么补检索硬性只有一轮**：多轮回路的收益递减极快而代价是线性的
        （每轮一次改写 + N 次召回 + 一次重排）。真正答不上来的问题，第二轮
        也答不上来——那属于知识库缺内容，正确的出口是转人工建工单（第 ⑧ 步
        的置信度闸门），而不是让检索在这里空转。回路的作用是捞回"检索角度
        没选对"的那一类，不是无限逼近。
        """
        queries, rewrite_failed = await self._rewritten(query)
        candidates = await self._recall(tool_name, queries, top_k, context)
        if not candidates:
            return ToolResult(success=False, data=[], tool_name=tool_name, error="所有子查询均无结果")

        outcome = await self._rerank(query, candidates, top_k)
        # **模型调用失败不是"模型判断证据不足"。** 改写或重排没调成的时候，这一轮
        # 没有任何关于证据够不够的判断——以前这里会退回分数阈值、判成"不足"，然后
        # 再跑一轮改写 + 重排：同一个故障打两遍（凭证被拒时就是 2 次 401 变 4 次），
        # 回来还对外报一句"证据不足"。失败就如实报"没判"（None），不补检。
        if rewrite_failed or outcome.failed:
            return ToolResult(success=True, data=outcome.items, tool_name=tool_name,
                              reranked=outcome.reranked, evidence_ok=None)

        evidence_ok = self._evidence_ok(outcome)
        if not self_correct or evidence_ok:
            return ToolResult(success=True, data=outcome.items, tool_name=tool_name,
                              reranked=outcome.reranked, evidence_ok=evidence_ok)

        # 自纠错：围绕缺口补检一轮，与第一轮的候选合并后重排。
        # 合并而不是替换——第一轮的结果未必没用，只是不够。
        logger.info(f"检索自纠错触发: {query!r}（缺口: {outcome.missing or '相关度不足'}）")
        more, rewrite_failed = await self._rewritten(query, hint=outcome.missing)
        if rewrite_failed:
            # 补检这一轮的改写没调成：手里只有原始查询，再检一遍拿回来的还是第一轮
            # 那批候选。第一轮的结论照旧算数，不为了一次失败的调用再重排一遍。
            return ToolResult(success=True, data=outcome.items, tool_name=tool_name,
                              reranked=outcome.reranked, evidence_ok=evidence_ok)
        extra = await self._recall(tool_name, more, top_k, context)
        merged = self._merge_unique(candidates + extra)
        retry = await self._rerank(query, merged, top_k)
        return ToolResult(
            success=True,
            data=retry.items,
            tool_name=tool_name,
            reranked=retry.reranked,
            corrected=True,
            evidence_ok=None if retry.failed else self._evidence_ok(retry),
        )

    async def _rewritten(self, query: str, hint: str = "") -> tuple[list[str], bool]:
        """改写，并说明那次模型调用成没成：`(子查询, 调用是否失败)`。"""
        queries = await self.rewrite_query(query, n=3, hint=hint)
        failed = bool(getattr(queries, "failed", False))
        if not failed:
            logger.info(f"查询改写{'（补检索）' if hint else ''}: {query!r} → {list(queries)}")
        return list(queries), failed

    async def _recall(
        self,
        tool_name: str,
        queries: list[str],
        top_k: int,
        context: dict[str, Any] | None,
    ) -> list[Any]:
        """并行召回所有子查询并合并去重。走缓存：补检索回合里重复的子查询不重复打库。"""
        recall_k = max(top_k, 5)
        results = await asyncio.gather(*[
            self.call(tool_name, {"query": q, "top_k": recall_k}, context, use_cache=True)
            for q in queries
        ], return_exceptions=True)

        rows: list[Any] = []
        for r in results:
            if isinstance(r, ToolResult) and r.success and isinstance(r.data, list):
                rows.extend(r.data)
        return self._merge_unique(rows)

    @staticmethod
    def _identity(item: Any) -> str:
        """
        一条候选的身份：内容本身，不含分数。

        之前是 md5(str(item))——item 里带着 score，同一个 chunk 被三个改写子查询
        召回时分数各不相同，哈希就各不相同，去重从来没生效过：top-5 里同一段
        出现三次，RAG 上下文被同一段填了三份，还占掉 top-k 预算。
        入库那边按内容哈希去重，这里对齐同一口径。
        """
        if isinstance(item, dict):
            body = item.get("content")
            if body is None:
                body = item.get("id")
            if body is None:
                body = json.dumps(
                    {k: v for k, v in item.items() if k != "score"},
                    sort_keys=True, ensure_ascii=False, default=str,
                )
            return hashlib.md5(str(body).encode()).hexdigest()
        return hashlib.md5(str(item).encode()).hexdigest()

    @classmethod
    def _merge_unique(cls, items: list[Any]) -> list[Any]:
        """
        按内容去重，保序（首次出现的位置），同一内容保留最高分。

        保序是因为第一个子查询就是用户原话，它召回的顺序最贴题；保留最高分
        是因为 _evidence_ok 的兜底口径看的是 max(score)，不该被先到的低分压住。
        """
        position: dict[str, int] = {}
        merged: list[Any] = []
        for item in items:
            key = cls._identity(item)
            at = position.get(key)
            if at is None:
                position[key] = len(merged)
                merged.append(item)
                continue
            kept = merged[at]
            if isinstance(item, dict) and isinstance(kept, dict):
                new, old = item.get("score"), kept.get("score")
                if isinstance(new, (int, float)) and (
                    not isinstance(old, (int, float)) or new > old
                ):
                    merged[at] = {**kept, "score": new}
        return merged

    # ── 证据充分性判定（自纠错的触发条件）────────────────────────────────────

    @staticmethod
    def _evidence_ok(outcome: RerankOutcome) -> bool:
        """
        判定这批证据够不够回答问题。

        优先用重排调用顺带返回的模型判定（零额外往返）；模型没给判定时
        （cross-encoder 重排、或候选太少没调模型）退回分数阈值。
        候选完全带不出分数的工具，判为充分——宁可不纠错，也不能凭一个
        取不到的信号去多打一整轮检索。
        """
        if not outcome.items:
            return False
        if outcome.sufficient is not None:
            return outcome.sufficient
        scores = [
            float(item["score"]) for item in outcome.items
            if isinstance(item, dict) and isinstance(item.get("score"), (int, float))
        ]
        return max(scores) >= EVIDENCE_MIN_SCORE if scores else True

    # ── 结果重排（解决召回不好）──────────────────────────────────────────────

    async def _rerank(self, query: str, items: list[Any], top_k: int) -> RerankOutcome:
        """
        对召回结果重新排序，优先 cross-encoder，回落 LLM listwise。

        解决问题：向量检索的相似度分数不等于"对用户有用"。cross-encoder 把
        query 和候选拼在一起过一遍前向，能建模两者的交互；LLM listwise 更慢
        更贵，但能一次看到全部候选、给出全局排序，并且顺带判定证据充分性。

        候选数不超过 top_k 时没有可排的东西，直接原样返回且 reranked=False——
        否则调用方会看到 reranked=True 但结果其实没经过任何重排。
        """
        if len(items) <= top_k:
            return RerankOutcome(items=items, reranked=False)

        ranked = await self._cross_encoder_rerank(query, items, top_k)
        if ranked is not None:
            return RerankOutcome(items=ranked, reranked=True)
        return await self._llm_rerank(query, items, top_k)

    async def _cross_encoder_rerank(
        self, query: str, items: list[Any], top_k: int
    ) -> list[Any] | None:
        """走远端 cross-encoder；未配置或失败返回 None，由调用方回落。"""
        if self._reranker is None or not getattr(self._reranker, "enabled", False):
            return None
        docs = [self._as_document(item) for item in items]
        ranked = await self._reranker.rerank(query, docs, top_k)
        if not ranked:
            return None
        return [items[idx] for idx, _ in ranked]

    @staticmethod
    def _as_document(item: Any, limit: int = 2000) -> str:
        """
        把候选压成给重排模型看的文本。dict 取标题 + 正文（正文最多 limit 字），
        其余原样序列化。
        """
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip()
            content = str(item.get("content", "")).strip()
            if title or content:
                return f"{title}\n{content[:limit]}"
        return json.dumps(item, ensure_ascii=False)[:limit]

    async def _llm_rerank(self, query: str, items: list[Any], top_k: int) -> RerankOutcome:
        """
        LLM listwise 重排，**同一次调用顺带返回证据充分性判定**。

        合并进同一个 JSON 而不是单开一次调用：判定所需的信息（查询 + 全部
        候选正文）和重排完全相同，模型已经读过一遍了，再问一次纯属重复付费。

        每条候选给的是标题 + 正文（PASSAGE_CHARS 字以内），不是整条记录的 JSON。
        以前是 json.dumps(item)[:200]：标题和键名先占掉三四十个字符，正文只剩
        一百六十来字，而一个分片可以有 500 字——条款里的条件和例外多半写在后半段，
        模型是在没读到它们的情况下排的序、判的"够不够"。prompt 因此变长，
        长度随候选数线性增长，上限是 候选数 × PASSAGE_CHARS。
        """
        items_text = "\n\n".join(
            f"[{i}] {self._as_document(item, PASSAGE_CHARS)}" for i, item in enumerate(items)
        )
        prompt = f"""根据用户查询对以下检索结果按相关性排序，并判断这些内容是否足以回答该问题。
用户查询: "{query}"
检索结果（每条以 [索引] 开头，第一行是标题，之后是正文）:
{items_text}

order 是按相关性降序排列的候选索引。
sufficient 判断标准：排在最前的内容能否直接支撑一个具体、有依据的回答；
只是话题相关但答不到点上，算 false。
missing 在 sufficient 为 false 时用一句话说明缺什么信息，充分时留空字符串。"""
        prompt = self._clean_text(prompt)

        try:
            payload = await structured_parse(
                self._client,
                output_format=RerankPayload,
                model=self._model, max_tokens=STRUCTURED_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            sufficient, missing = payload.sufficient, payload.missing[:200]
            # 索引仍然要过滤：schema 保证得了"是一个整数数组"，保证不了每个整数都
            # 落在候选范围内。越界索引会 IndexError 掉进下面的 except，把一次本可
            # 以用的重排变成"返回原始顺序"。
            reordered = [items[i] for i in payload.order if 0 <= i < len(items)]
            if not reordered:
                return RerankOutcome(items=items[:top_k], reranked=False,
                                     sufficient=sufficient, missing=missing)
            return RerankOutcome(items=reordered[:top_k], reranked=True,
                                 sufficient=sufficient, missing=missing)
        except Exception as ex:
            log_model_failure(logger, "重排失败，返回原始顺序", ex)
            return RerankOutcome(items=items[:top_k], reranked=False, failed=True)

    # ── 缓存 ──────────────────────────────────────────────────────────────────

    def _cache_key(self, name: str, params: dict, rerank_top_k: int = 0) -> str:
        payload = {"params": params, "rerank_top_k": rerank_top_k}
        return f"{name}:{hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()}"

    def _get_cache(self, name: str, params: dict, rerank_top_k: int = 0) -> tuple[Any, bool] | None:
        key = self._cache_key(name, params, rerank_top_k)
        if key in self._cache:
            data, expire_at, reranked = self._cache[key]
            if time.monotonic() < expire_at:
                return data, reranked
            del self._cache[key]
        return None

    def _set_cache(
        self,
        name: str,
        params: dict,
        data: Any,
        ttl: float,
        rerank_top_k: int = 0,
        reranked: bool = False,
    ) -> None:
        if len(self._cache) >= 5000:
            # 清掉最旧的 1/4
            for k in list(self._cache)[:1250]:
                del self._cache[k]
        self._cache[self._cache_key(name, params, rerank_top_k)] = (data, time.monotonic() + ttl, reranked)

    # ── 参数校验 ──────────────────────────────────────────────────────────────

    _TYPE_MAP = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}

    def _validate_params(self, tool: Tool, params: dict[str, Any]) -> None:
        """根据工具的 JSON Schema 校验参数，不合法时抛出 ValueError。"""
        schema = tool.schema
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for name in required:
            if name not in params:
                raise ValueError(f"工具 {tool.name} 缺少必需参数: {name}")

        for key, value in params.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type and expected_type in self._TYPE_MAP:
                    if not isinstance(value, self._TYPE_MAP[expected_type]):
                        raise ValueError(
                            f"工具 {tool.name} 参数 {key} 类型错误: 期望 {expected_type}，实际 {type(value).__name__}"
                        )

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 LLM 请求编码失败。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        return {
            name: {
                "total": t.stats.total,
                "success_rate": round(t.stats.success_rate, 3),
                "avg_latency_ms": round(t.stats.avg_latency_ms, 1),
                "consecutive_fails": t.stats.consecutive_fails,
                "circuit_state": t.breaker.state.value,
            }
            for name, t in self._tools.items()
        }
