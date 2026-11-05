from __future__ import annotations

import threading
from typing import Any, Dict, Literal, Optional

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .model_settings import OVERRIDABLE, key_field, load_store, url_field

Provider = Literal["openai", "anthropic"]

# 默认取每家最便宜的在产型号：主链路一次查询要打 5 次模型，
# 默认值贵一档，跑一轮 benchmark 的成本就差出一个量级。
# 想要更强的 SQL 生成，单独覆盖 AI_SQL_CODER_MODEL 即可（如 gpt-5.6-terra）。
DEFAULT_CHAT_MODEL = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-sonnet-5",
}
DEFAULT_CODER_MODEL = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-sonnet-5",
}


CHAT_PATH = {
    "openai": "chat/completions",
    "anthropic": "messages",
}


def build_endpoint(base_url: str, path: str) -> str:
    """把 BASE_URL 拼成完整端点，容忍它带不带 `/v1` 后缀。

    OPENAI_BASE_URL 通常写到 `/v1`（https://relay.example.com/v1），
    ANTHROPIC_BASE_URL 通常不带（https://relay.example.com），两种都要能用。
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/{path.lstrip('/')}"


class SqlAgentSettings(BaseSettings):
    """
    全局配置。

    统一收口原先散落在各模块构造函数里的 `os.getenv` 调用：
    - 默认前缀 `AI_SQL_`（例如 `AI_SQL_PGVECTOR_DSN`）；
    - 模型凭证直接认 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 这类通用命名；
    - 支持项目根目录下的 `.env` 文件。

    通过 `get_settings()` 获取单例，测试里用 `reload_settings()` 重新读取。
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_SQL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # model_provider 是配置项而不是 pydantic 的模型元数据，关掉 model_ 前缀保护
        protected_namespaces=(),
    )

    # ---- 模型厂商选择 ----
    model_provider: Provider = Field(
        default="openai",
        validation_alias=AliasChoices("AI_SQL_MODEL_PROVIDER", "MODEL_PROVIDER"),
    )
    """
    主链路（关键词抽取→CoT 规划→SQL 生成）用哪套协议：

    - `openai`：OpenAI 兼容协议（OpenAI 官方、中转站、阿里云百炼、vLLM、Ollama 都是这套）；
    - `anthropic`：Anthropic Messages 协议（`/v1/messages` + `x-api-key`）。

    只改这一个变量就能整体切换，代码不用动。Embedding / Rerank 始终走
    OpenAI 兼容协议（Anthropic 不提供这两种接口）。
    """

    # ---- OpenAI 兼容通道凭证 ----
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("AI_SQL_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )

    # ---- Anthropic 通道凭证 ----
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_SQL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com",
        validation_alias=AliasChoices(
            "AI_SQL_ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL"
        ),
    )
    anthropic_version: str = "2023-06-01"
    """Messages 接口的 `anthropic-version` 请求头。"""

    max_output_tokens: int = 4096
    """Anthropic Messages 协议要求显式给 `max_tokens`，这里是默认值。"""

    max_completion_tokens: int = 8192
    """
    OpenAI 推理模型（GPT-5 起、o 系列）一次调用的输出上限，**推理过程也算在里面**。

    比 `max_output_tokens` 大一档：上限只够写回答的话，推理一长就一个字也剩不下，
    表现为 `finish_reason=length`、正文为空。只是上限，用不到不花钱。
    """

    reasoning_effort: str = "low"
    """
    OpenAI 推理模型想多久（`reasoning_effort`）：minimal / low / medium / high，空串 = 不传、
    用模型自己的默认档（通常是 medium）。

    默认 low：一个问题要连着调五六次模型（抽词、路由、规划、生成、自检），每一次多想几秒，
    整题就慢出几十秒，而调用方（ai-helpdesk）等的上限是 45 秒。这几步都是有明确格式、
    有 Schema 可对照的任务，不靠长推理。中转站或模型不认这个参数时自动去掉。
    对 Anthropic 和不是推理模型的不起作用。
    """

    embedding_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_SQL_EMBEDDING_API_KEY", "EMBEDDING_API_KEY"
        ),
    )
    """
    Embedding 服务凭证。留空时回落到 `openai_api_key`。

    单独拆出来是因为 Chat 与 Embedding 可能来自不同厂商——例如主链路走
    Anthropic 时，Embedding 只能另配一家（或不配，主链路本就走本地哈希向量）。
    """

    rerank_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_RERANK_API_KEY", "DASHSCOPE_API_KEY"),
    )
    """Rerank（阿里云百炼 qwen3-rerank）凭证，留空时回落到 `openai_api_key`。"""

    dashscope_workspace_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_SQL_DASHSCOPE_WORKSPACE_ID", "DASHSCOPE_WORKSPACE_ID"
        ),
    )
    """配了才启用真实 Rerank，否则字段级精排走本地词面打分。"""

    # ---- 模型服务地址与模型名 ----
    chat_url: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_CHAT_URL", "MODEL_CHAT_URL"),
    )
    """
    Chat 端点。留空时按 `model_provider` 从 BASE_URL 自动拼：

    - openai：    `{OPENAI_BASE_URL}/chat/completions`
    - anthropic： `{ANTHROPIC_BASE_URL}/v1/messages`

    显式配置时优先级最高，用于内网 vLLM 这类非标准路径。
    """
    embedding_url: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_EMBEDDING_URL", "MODEL_EMBEDDING_URL"),
    )
    """
    Embedding 端点，留空时取 `{OPENAI_BASE_URL}/embeddings`。

    `embedding_backend=remote` 时 Schema 检索用它；`local`（默认）时主链路用本地哈希向量，
    不调这个接口。离线建 pgvector 索引的脚本（`ai_sql_agent.indexing`）一直用它。
    """

    embedding_backend: Literal["local", "remote"] = "local"
    """
    Schema 检索（库 / 表 / 字段三层的向量召回）用哪种向量：

    - `local`：本地哈希向量。不调任何服务，但没有语义——「多少钱」和「金额」对不上，
      口语问法主要靠关键词召回和抽词时的术语映射；
    - `remote`：调 `embedding_url`（OpenAI 兼容的 /embeddings），用 `embedding_model`、
      `embedding_dimensions`。启动和 Schema 重建时把全部表、字段说明算一遍向量；
      服务调不通时退回本地哈希向量，服务照常起，`/admin/model` 里能看到原因。

    也可以在页面的「设置 → 高级设置 → Embedding」里配置，保存时会先用新模型把索引建好再换上。
    """

    embedding_batch_size: int = Field(default=10, ge=1, le=2048)
    """一次 /embeddings 请求带几段文本。百炼最多 10；OpenAI 可以到 2048，调大建索引更快。"""

    embedding_timeout_seconds: float = Field(default=15.0, gt=0)
    """单次 Embedding 请求的超时。查询时每个问题要等它一次，比对话调用的超时短。"""
    rerank_url: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_RERANK_URL", "DASHSCOPE_RERANK_URL"),
    )

    chat_model: str = ""
    """规划/路由用的模型，留空时按厂商取默认值。"""

    coder_model: str = ""
    """SQL 生成用的模型，留空时按厂商取默认值。"""

    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    rerank_model: str = "qwen3-rerank"

    # ---- 从页面配置模型连接 ----
    model_settings_enabled: bool = True
    """
    允许管理员在页面的「设置 → 高级设置 → 模型连接」里换模型连接（协议、接口地址、Key、
    两个模型名），保存后立刻生效，不用重启。

    页面保存的那一份压过这里的环境变量和 `.env`，直到有人在页面上点「恢复为服务端配置」。
    关掉之后：已经保存的那份不再读，页面只显示当前用的是什么，`/admin/model` 的修改接口返回 403。

    谁能改：开了鉴权的实例要管理员 Key；没开鉴权的实例只接受本机直接发来的请求
    （见 `ai_sql_agent.api.model_admin`）。多副本部署各自的文件互不相通，应当关掉它，
    继续用环境变量或密钥管理来配。
    """

    model_settings_path: str = "runtime_data/model_settings.json"
    """
    页面保存的模型连接放在哪个文件里。里面有明文的 Key（和 `.env` 一样），权限是 0600。
    相对路径按进程的工作目录算；留空等于关掉 `model_settings_enabled`。
    """

    # ---- HTTP 调用治理 ----
    http_timeout_seconds: float = 60.0
    http_max_retries: int = 2
    """可重试错误（429/5xx/超时/连接失败）的额外重试次数，0 表示不重试。"""
    http_backoff_base_seconds: float = 0.5
    http_backoff_max_seconds: float = 8.0

    # ---- 运行模式 ----
    app_env: Literal["development", "production"] = "development"

    # 部署范围。**空串是"未声明"，不是默认值**——生产模式下未声明即拒绝启动，
    # 见 src/ai_sql_agent/api/app.py::check_production_readiness。给默认值会把"漏配了"
    # 和"想清楚了配成这个"合并成同一个状态，而这两件事的风险完全不同。
    # 同样的理由这个仓库用过一次：row_filters 的 predicate: "*" 是**显式声明**
    # "这张表不做行隔离"，与"没配"是两回事。
    deployment_scope: Literal["", "internal_single_tenant", "multi_tenant"] = ""
    """
    生产模式（`AI_SQL_APP_ENV=production`）会把一批「开发下可以将就」的配置
    改判为启动失败：Mock 模型、示例语义层、鉴权关闭、未知角色放开、业务库账号
    不是只读。见 `ai_sql_agent.api.app.check_production_readiness`。

    默认 development：本地跑 demo 与测试不该被这些检查挡住。
    """

    # ---- 鉴权与数据权限 ----
    auth_mode: Literal["disabled", "api_key"] = "disabled"
    """disabled 仅用于本地开发（启动会打警告）；生产部署设为 api_key。"""

    unknown_role_policy: Literal["deny", "full_access"] = "deny"
    """
    API Key 的角色在 `policies` 里一条都没匹配上时给什么权限。

    默认 `deny`。`full_access` 是早期的行为——用一个不存在的
    角色就能拿到全部权限。保留这个开关只为让老部署有个显式的过渡位置：它必须是
    有人主动写下来的，而不是默认值。生产模式下配成 `full_access` 直接启动失败。
    """

    api_keys: str = ""
    """API Key 配置 JSON 字符串，适合本地或极简部署。"""

    api_keys_file: str = ""
    """API Key 配置文件路径，推荐（K8s Secret / Docker secret 挂载）。"""

    # ---- A2A ----
    a2a_streaming: bool = True
    """
    A2A `message/stream` 开关。关 = Card 报 streaming:false、message/stream 返回
    -32004，调用方退回 message/send（契约 §6.2 的回滚路径）。

    默认开。契约 v1.3 曾把「调用方先补好 -32004 降级分支」列为硬阻断，2026-09-03
    两条路都做过实机回滚演练才翻开的：
      · Card 路径——开关关掉、调用方不重启，Card 过期后读到 streaming:false，
        直接走 send，答案正确；
      · -32004 路径——把调用方的 Card 缓存强行写成 streaming:true（模拟「开关刚关，
        手上还是旧 Card」），发 message/stream 收到 -32004，自动改发 send。
    两条都验过，关这个开关是降级而不是故障。
    """

    # ---- 限流与配额 ----
    default_rate_limit_per_minute: int = 60
    default_daily_query_quota: int = 0
    """0 表示不限制。"""

    default_daily_token_quota: int = 0

    # ---- Schema 热重建 ----
    schema_refresh_seconds: float = 0.0
    """业务库 Schema 轮询间隔，0 表示不开启（本地开发默认）。生产建议 300。"""

    # ---- 审计 ----
    audit_enabled: bool = True
    audit_db_path: str = "runtime_data/audit.db"
    """未配置 pgvector DSN 时审计落这个 SQLite 文件。"""

    # ---- 运行记录（run）与事件日志 ----
    runs_enabled: bool = True
    """
    每次请求落一条 run 记录和一份事件日志（`ai_sql_agent.runs`）。关掉时事件只在进程内流转，
    `/runs` 查不到历史。这份数据丢了不影响查询。
    """

    runs_db_path: str = "runtime_data/runs.db"
    """未配置 pgvector DSN 时 run 记录落这个 SQLite 文件（与审计一样，配了 DSN 就落 PostgreSQL）。"""

    runs_retention_days: float = 7.0
    """run 结束多少天后连同事件一起删掉。"""

    run_event_attr_max_bytes: int = 16384
    """单条事件 attrs 序列化后的上限；SQL 最多留 8 KiB，其余字符串 1 KiB，超出部分截断。"""

    cancel_on_disconnect: bool = True
    """
    调用方断开连接时取消这次 run（三条入口：`/query`、A2A send、A2A stream）。

    默认开：a2a-contract v1.13 §5.4 写的是"调用方断开后，我方在下一个检查点停止"。关掉就回到
    v1.12 的行为（断开后不中止，跑完为止）。控制台的停止按钮走 `POST /runs/{id}/cancel`，
    不受这个开关影响。
    """

    run_deadline_seconds: float = 0.0
    """
    一次 run 的总时长上限，超过就在下一个检查点取消。0 = 不限（默认）。

    要开的话必须大于调用方的硬上限（今天 90 s）：契约 §5.3 说我方不会为了塞进调用方的预算
    而截断查询，这个上限是保护服务自己的资源，不是替调用方计时。
    """

    # ---- 熔断 ----
    circuit_breaker_enabled: bool = True
    circuit_failure_threshold: int = 5
    """连续多少次基础设施类失败后跳闸（超时/连接失败/5xx/限流）。"""

    circuit_reset_seconds: float = 30.0
    """跳闸后多久放一次试探请求。"""

    # ---- 业务语义元数据 ----
    semantic_dir: str = ""
    """
    业务语义元数据目录（每个库一个 YAML，见 ai_sql_agent.semantic）。

    留空时使用内置的四个演示库元数据——**接真实业务库必须配置这一项**：
    模型认识业务全靠这份元数据，缺了它检索只能按字段名字面匹配，
    口语问法基本召不回来，现象是「模型答不准」，实际是没告诉它业务是什么。

        python -m ai_sql_agent.semantic export semantic/     # 导出模板
        python -m ai_sql_agent.semantic validate semantic/   # 校验并与真实库比对
        AI_SQL_SEMANTIC_DIR=semantic/
    """

    # ---- 用户反馈 ----
    feedback_enabled: bool = True
    """
    收集用户对答案的对错反馈。

    关掉之后 /feedback 返回 404，认证 SQL 复用与 `python -m ai_sql_agent.feedback`
    也就没有数据来源了。
    """

    certified_sql_enabled: bool = True
    """
    把用户标记「正确」的查询作为认证范例注入生成 Prompt。

    只用同一用户/同一权限下确认过的 SQL，且执行前照常过权限校验。
    """

    certified_sql_top_k: int = 2
    """单次查询最多注入几条认证范例。多了会挤占 Schema 的 Token 预算。"""

    # ---- 存储 ----
    pgvector_dsn: str = ""
    """配置后 Schema 向量索引与长期记忆向量索引都切到 PostgreSQL + pgvector。"""
    memory_db_path: str = "runtime_data/memory.db"

    # ---- SQL 执行治理 ----
    max_rows: int = Field(default=1000, ge=1)
    """
    单条查询最多返回多少行，超出的部分丢弃，结果里标 `truncated`。

    API 启动时经 `PipelineConfig` 交给每个库的执行器（`api/app.py` 的 `lifespan`），
    启动日志 `api.startup` 打出来的就是执行器在用的数。

    这个配置项以前只进了启动日志：默认值写着 10000，实际生效的一直是 `PipelineConfig`
    的 1000。默认值按实际生效的那个对齐，所以不设这个变量的部署行为不变。

    调大之前先看这些行去了哪儿：`/query` 响应和 A2A 的 data part 带全部行，
    对话记忆把整份结果存进记忆库；模型会话摘要只读取每条消息的限长摘录
    （`SummaryBudgetConfig`），不是完整结果。主动保存到个人知识库的摘要入口
    仍会读取完整内容和元信息，没有这项限长保护。
    没有「0 = 不限」这个取值，0 和负数在读配置时直接报错。
    """

    max_execution_seconds: float = Field(default=10.0, gt=0)
    """
    单条查询最长执行多久，超时由数据库侧中止（SQLite 的 progress handler、
    PostgreSQL 的 `statement_timeout`）。与 `max_rows` 一样经 `PipelineConfig` 生效，
    默认值同样从没生效过的 30 对齐到实际生效的 10。

    必须大于 0：PostgreSQL 把 `statement_timeout = 0` 当成「不限时」。
    """

    max_result_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    """
    单条查询返回的值加起来最多多少字节，超出的行被丢弃，结果里标 `truncated`
    （`row_limit` 是实际返回的行数）。

    行数上限管不住体积：每行 5 MB 的话 40 行就是 200 MB，它先进执行器内存，再进
    HTTP 响应、A2A 数据段、对话记忆库和下一步的提示词。

    默认 8 MiB 的依据：`max_rows` 的默认 1000 行里，每行约 1 KB 文本是偏重的业务表
    （约 1 MB），8 MiB 给到它的 8 倍；预览一页至多 100 行，远在其下。只有长文本、
    备注、JSON 大字段成批取出时才会碰到，碰到时结果标「已截断」，调用方
    加条件或只取需要的列即可。第一行就超过它时查询报错，不返回半行。

    口径是各个值的字节数之和（字符串按 UTF-8、二进制按原长、数字按 8 字节），
    不含列名和序列化的标点。SQLite 与 PostgreSQL 执行器都按它截；
    PostgreSQL 一侧只管「返回多少」，见 `PostgresMCPExecutor`。
    """

    max_value_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    """
    SQLite 单个字符串 / 二进制值 / 一行的最大长度（`SQLITE_LIMIT_LENGTH`），超出时
    语句在分配之前就报错。progress handler 打断不了单次函数调用，
    `SELECT hex(randomblob(150000000))` 这种写法只有这个上限管得住。

    默认与 `max_result_bytes` 相同：单个值比整份结果的预算还大时，它本来就
    返回不出去。调高之前注意——库里已有超过这个长度的行，`SELECT` 读它们也会报错。
    只对 SQLite 生效，PostgreSQL 没有对应的会话参数。
    """

    db_pool_max_connections: int = 8
    """
    单个 PostgreSQL 业务库的连接池上限。0 表示不用池，每次现建连接
    （2026-09-04 之前的行为）。
    """

    db_pool_idle_seconds: float = 300.0
    """池里的空闲连接超过这个时长就丢弃重建，避免拿到被服务端掐掉的死连接。"""

    # ---- 数据浏览（目录 + 只读预览）----
    catalog_enabled: bool = True
    """
    关掉之后 /catalog 一组接口返回 404，页面的「数据浏览」显示未启用。

    预览走的是和问答同一个执行器、同一份表列权限，不是另一条数据通路；
    这个开关只是给「只想开放问答、不想让人翻表」的部署留一个位置。
    """

    catalog_preview_max_page_size: int = 100
    """
    预览一页最多多少行。请求里要得更多时按这个数截；执行器的行数上限（`max_rows`）
    比它还小时按更小的那个算。响应里的 `page_size` 是实际用的页大小。
    """

    catalog_preview_max_offset: int = 10000
    """
    预览最多能翻到第几行。目录是给人认识数据用的，不是导出通道：
    翻到这么深还没找到，该做的是加筛选条件或者直接提问。
    """

    catalog_preview_timeout_seconds: float = 5.0
    """单次预览查询的时长上限，比 `max_execution_seconds` 紧——超了就让人加条件。"""

    catalog_rate_limit_per_minute: int = 120
    """
    目录与预览的限流，按调用方单独计数，**不占问答的额度**。
    翻页是连续点击，和提问共用一个桶的话，翻几页表就会把自己提问的额度翻没。
    """

    catalog_structure_ttl_seconds: float = 30.0
    """表结构的缓存时长。每次打开目录都去读一遍系统表没有必要。"""

    catalog_profile_enabled: bool = True
    """
    表概况（行数、各字段的取值分布、按月的记录数）开不开。页面的分布图和时间轴靠它。

    概况只做聚合、不返回记录，权限与预览同一份；但每一项都是一遍全表扫描，
    几千万行的表上会一条条跑到超时（`catalog_preview_timeout_seconds`）。
    那样的部署可以只关它：目录和预览照常，页面不再显示统计。
    """

    # ---- A2A 流式资源 ----
    a2a_stream_max_concurrent: int = 8
    """
    同时在跑的 message/stream 上限，超出直接拒（HTTP 429 + -32029，契约 v1.13 §5.5），不排队。

    排队只会把压力从「拒绝」变成「全都超时」，而调用方的 90s 硬上限一到
    照样全丢——不如在流打开之前说清楚。
    """

    a2a_stream_worker_threads: int = 8
    """
    跑同步 pipeline 的**专用**线程池大小。

    以前生产者（`asyncio.to_thread`）和消费者（`run_in_executor(None, …)`）
    抢同一个默认线程池：把默认池限成 2 线程、开 2 条流，两个消费者就把池占满，
    pipeline 启动数为 0（复现过）。专用池 + 消费者不再占线程，两边就不再争。
    """

    a2a_stream_queue_size: int = 256
    """
    事件队列上限（背压）。以前是无界 `queue.Queue`：客户端读得慢时事件只会
    在内存里堆着，谁都不知道堆了多少。
    """

    # ---- 日志 ----
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    # 页面保存的模型连接生效时，这两项记着「被压过之前」的配置和页面存的那一份。
    # 都不是配置项：不从环境变量读，也不出现在 model_dump 里。
    _server_side: Optional["SqlAgentSettings"] = PrivateAttr(default=None)
    _page_store: Dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _fill_provider_defaults(self) -> "SqlAgentSettings":
        """按选中的厂商补齐端点与模型名，显式配置永远优先。"""
        if not self.chat_url:
            self.chat_url = build_endpoint(
                self.base_url_of(self.model_provider), CHAT_PATH[self.model_provider]
            )
        if not self.embedding_url:
            self.embedding_url = build_endpoint(self.openai_base_url, "embeddings")
        if not self.chat_model:
            self.chat_model = DEFAULT_CHAT_MODEL[self.model_provider]
        if not self.coder_model:
            self.coder_model = DEFAULT_CODER_MODEL[self.model_provider]
        return self

    def api_key_of(self, provider: str) -> str:
        """某一家协议当前配的 Key（没配是空串）。"""
        return str(getattr(self, key_field(provider), ""))

    def base_url_of(self, provider: str) -> str:
        """某一家协议当前配的接口地址。"""
        return str(getattr(self, url_field(provider), ""))

    def resolved_chat_api_key(self) -> str:
        """主链路凭证：按选中的厂商取对应的 Key。"""
        return self.api_key_of(self.model_provider)

    @property
    def server_side(self) -> "SqlAgentSettings":
        """
        环境变量和 `.env` 给出的那份配置。页面没有保存过模型连接时就是它自己。

        页面上「恢复为服务端配置」会回到它；Embedding / Rerank 的凭证也始终从它取。
        """
        return self._server_side or self

    @property
    def page_store(self) -> Dict[str, Any]:
        """页面保存的模型连接（`values` / `saved_at` / `saved_by`）；没有在用时是空的。"""
        return self._page_store

    @property
    def model_settings_file(self) -> str:
        """页面保存的模型连接读写哪个文件；这个功能关着时是空串。"""
        return self.model_settings_path.strip() if self.model_settings_enabled else ""

    def resolved_embedding_api_key(self) -> str:
        """
        Embedding 凭证。页面保存过 Embedding 连接就是那一节里的 Key；否则单独配了就用单独的，
        再否则回落到 OpenAI 兼容通道。

        回落取的是**服务端配置**里的那一把：页面换主链路的 Key 不该顺带把它发给
        Embedding 的地址（那个地址可能是另一台主机）。
        """
        if self._page_store.get("embedding"):
            return self.embedding_api_key
        source = self.server_side
        return source.embedding_api_key or source.openai_api_key

    def resolved_rerank_api_key(self) -> str:
        """Rerank 凭证：单独配了就用单独的，否则回落到 OpenAI 兼容通道（同样取服务端配置）。"""
        source = self.server_side
        return source.rerank_api_key or source.openai_api_key

    @property
    def has_model_credentials(self) -> bool:
        return bool(self.resolved_chat_api_key())

    def resolved_rerank_url(self) -> str:
        """Rerank 走业务空间专属域名，未显式配置时按 workspace_id 拼接。"""
        if self.rerank_url:
            return self.rerank_url
        if self.dashscope_workspace_id:
            return (
                f"https://{self.dashscope_workspace_id}"
                f".cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks"
            )
        return ""


def apply_model_overrides(base: SqlAgentSettings, store: Dict[str, Any]) -> SqlAgentSettings:
    """
    在服务端配置上叠一份页面保存的模型连接，返回新的配置对象（`base` 不动）。

    只换主链路的这几项：协议、这家协议的接口地址与 Key、两个模型名。Chat 端点一律由页面的
    接口地址重新拼——服务端显式配过 `AI_SQL_CHAT_URL` 也不沿用，否则页面的 Key 会被发到
    那个地址去。Embedding 的端点在 `base` 里已经解析好了，原样带过来，不跟着页面变。
    """
    values = store.get("values") or {}
    provider = values["model_provider"]
    merged = base.model_copy(
        update={name: values[name] for name in OVERRIDABLE if values.get(name)}
    )
    merged.chat_url = build_endpoint(merged.base_url_of(provider), CHAT_PATH[provider])
    merged.chat_model = values.get("chat_model") or DEFAULT_CHAT_MODEL[provider]
    merged.coder_model = values.get("coder_model") or DEFAULT_CODER_MODEL[provider]
    merged._server_side = base
    merged._page_store = dict(store)
    return merged


def apply_embedding_overrides(
    current: SqlAgentSettings, entry: Dict[str, Any]
) -> SqlAgentSettings:
    """
    在配置上叠一份页面保存的 Embedding 连接，返回新的配置对象（`current` 不动）。

    页面保存的 Embedding 一律是远端的：地址拼成 `/embeddings` 端点，Key 用那一节自己的。
    """
    values = entry["values"]
    return current.model_copy(
        update={
            "embedding_backend": "remote",
            "embedding_url": build_endpoint(values["base_url"], "embeddings"),
            "embedding_api_key": values["api_key"],
            "embedding_model": values["model"],
            "embedding_dimensions": int(values["dimensions"]),
        }
    )


def _with_page_store(base: SqlAgentSettings) -> SqlAgentSettings:
    """
    服务端配置 → 当前生效的配置：页面保存过模型连接（主链路 / Embedding）就叠上去。

    那个文件读不了、某一节不完整时照常返回服务端配置——服务必须起得来；
    问题记在 `page_store["problem"]` / `page_store["embedding_problem"]` 上，
    启动日志和 `/admin/model` 会说出来。
    """
    path = base.model_settings_file
    store = load_store(path) if path else {}
    current = base
    if store.get("values"):
        current = apply_model_overrides(current, store)
    if store.get("embedding"):
        current = apply_embedding_overrides(current, store["embedding"])
    if current is base:
        base._page_store = {
            name: store[name] for name in ("problem", "embedding_problem") if store.get(name)
        }
        return base
    current._server_side = base
    current._page_store = dict(store)
    return current


_current: Optional[SqlAgentSettings] = None
_current_lock = threading.Lock()


def get_settings() -> SqlAgentSettings:
    """当前生效的配置（进程内只建一次）：环境变量和 `.env`，再叠上页面保存的模型连接。"""
    global _current
    settings = _current
    if settings is None:
        with _current_lock:
            if _current is None:
                _current = _with_page_store(SqlAgentSettings())
            settings = _current
    return settings


def reload_settings() -> SqlAgentSettings:
    """丢掉已经建好的配置，重新读环境变量、`.env` 和页面保存的模型连接。供测试使用。"""
    global _current
    with _current_lock:
        _current = None
    return get_settings()


def reload_model_settings() -> SqlAgentSettings:
    """
    只重读页面保存的模型连接；环境变量和 `.env` 那一半沿用进程里已经读到的。

    页面保存之后用它，而不是 `reload_settings()`：后者会把 `.env` 整个重读一遍。
    服务启动之后有人改过 `.env` 里别的配置的话，那些改动会被「半生效」——
    有的组件每次请求都读配置，有的启动时就把值抄走了。
    """
    global _current
    with _current_lock:
        base = (_current or SqlAgentSettings()).server_side
        _current = _with_page_store(base)
        return _current
