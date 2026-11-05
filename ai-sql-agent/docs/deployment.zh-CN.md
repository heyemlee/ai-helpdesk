# AI SQL Agent 部署与运维

[English](../README.md) | **简体中文**

面向公司内部自用部署：单实例、无多租户、服务自校验身份。

## 一、首次部署

```bash
# 1. 生成 API Key 配置
cp deploy/api_keys.example.json deploy/api_keys.json
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 每个调用方生成一个
# 编辑 deploy/api_keys.json，替换 key 并按团队实际情况改 roles / policies

# 2. 配置模型凭证
cat > .env <<'ENV'
AI_SQL_MODEL_PROVIDER=openai          # 或 anthropic
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://relay.example.com/v1
# ANTHROPIC_API_KEY=sk-ant-xxx
# ANTHROPIC_BASE_URL=https://relay.example.com
POSTGRES_PASSWORD=改成强密码
ENV

# 3. 起服务
docker compose --project-directory . -f deploy/docker-compose.prod.yml up -d --build

# 4. 验收
curl -s localhost:8000/health
curl -s localhost:8000/readyz | python -m json.tool
```

`deploy/api_keys.json` 已在 `.dockerignore` 里，不会进镜像，只以只读方式挂载。
换 Key 只需改文件并 `docker compose --project-directory . -f deploy/docker-compose.prod.yml restart app`。

## 二、配置项

| 环境变量 | 生产建议 | 说明 |
| --- | --- | --- |
| `AI_SQL_MODEL_PROVIDER` | `openai` | `openai`=OpenAI 兼容协议，`anthropic`=Messages 协议 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 按厂商填 | provider=openai 时的凭证与端点 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | 按厂商填 | provider=anthropic 时的凭证与端点 |
| `AI_SQL_MODEL_SETTINGS_ENABLED` | 单实例 `true`，多副本 `false` | 允许管理员在页面上修改模型连接，见 §八之二「从页面修改模型连接」 |
| `AI_SQL_MODEL_SETTINGS_PATH` | `runtime_data/model_settings.json` | 页面保存的模型连接放在哪里。文件里有明文 Key，权限 0600 |
| `AI_SQL_AUTH_MODE` | `api_key` | `disabled` 会放行所有请求，只允许本地开发 |
| `AI_SQL_API_KEYS_FILE` | `/run/secrets/api_keys.json` | Key 与数据权限策略 |
| `AI_SQL_SEMANTIC_DIR` | `semantic/` | 业务语义元数据目录。**接真实库必配**，见 §二·六 |
| `AI_SQL_FEEDBACK_ENABLED` | `true` | 收集用户对回答的对错判定 |
| `AI_SQL_CERTIFIED_SQL_ENABLED` | `true` | 把确认正确的查询作为参考写法复用 |
| `AI_SQL_PGVECTOR_DSN` | 指向内网 PG | Schema 向量、长期记忆、审计共用 |
| `AI_SQL_MAX_ROWS` | `1000` | 单查询返回行数上限；超出的行丢弃，该步骤标 `truncated`。见下方「执行上限」 |
| `AI_SQL_MAX_EXECUTION_SECONDS` | `10` | 单查询执行超时（数据库侧中断） |
| `AI_SQL_MAX_RESULT_BYTES` | `8388608` | 单查询返回的值加起来的字节上限；超出的行丢弃，该步骤标 `truncated`。见下方「执行上限」 |
| `AI_SQL_MAX_VALUE_BYTES` | `8388608` | 仅 SQLite：单个字符串 / 二进制值 / 一行的最大长度（`SQLITE_LIMIT_LENGTH`），超出则语句报错中止 |
| `AI_SQL_HTTP_MAX_RETRIES` | `2` | 模型 429/5xx 退避重试次数 |
| `AI_SQL_DEFAULT_RATE_LIMIT_PER_MINUTE` | `60` | 未在 Key 上单独配置时的默认限流 |
| `AI_SQL_DEFAULT_DAILY_TOKEN_QUOTA` | 按预算设 | 0 表示不限，建议设上限保护模型账单 |
| `AI_SQL_SCHEMA_REFRESH_SECONDS` | `300` | Schema 变更轮询间隔，0 为关闭 |
| `AI_SQL_RUNS_ENABLED` | `true` | 每次请求一条运行记录和一份按序号的事件日志（`GET /runs/{id}/events`）。配了 `AI_SQL_PGVECTOR_DSN` 就落 PostgreSQL，否则落 `AI_SQL_RUNS_DB_PATH`（默认 `runtime_data/runs.db`）。丢了只是没有历史，不影响查询 |
| `AI_SQL_RUNS_RETENTION_DAYS` | `7` | 运行结束多少天后连同事件一起删掉 |
| `AI_SQL_CANCEL_ON_DISCONNECT` | `true` | 调用方断开后，在下一个检查点取消这次运行（A2A 契约 v1.13 §5.4）。设为 `false` 就跑完为止，和 v1.12 时一样；控制台的停止按钮走 `/runs`，不受它影响 |
| `AI_SQL_RUN_DEADLINE_SECONDS` | `0` | 一次运行的总时长上限，在下一个检查点生效；0 = 不限。要开就比调用方自己的上限大 |
| `AI_SQL_LOG_FORMAT` | `json` | 便于日志系统采集 |

### 执行上限

`AI_SQL_MAX_ROWS` 与 `AI_SQL_MAX_EXECUTION_SECONDS` 在 API 启动时交给每个库的执行器，
启动日志 `api.startup` 打出来的就是实际生效的值。不设时是 1000 行、10 秒。

- **升级时先看一眼自己的配置。** 更早的版本里这两个变量只进了启动日志：
  不管配成什么，查询一直按 1000 行 / 10 秒执行。本仓库的 compose 文件以前写的是
  `10000` / `30`；照着它抄出去的 compose，或者还留着这两个数的 `.env`，
  升级后第一次重启就会生效。
- **字节上限管的是体积，行数上限管不了它。** 一行 5 MB 也只是一行。
  `AI_SQL_MAX_RESULT_BYTES` 限制返回的所有值加起来多少字节（被截时标 `truncated`，
  `row_limit` 是实际返回的行数）；第一行就超过它时报错，不返回空页。
  `AI_SQL_MAX_VALUE_BYTES` 就是 SQLite 的 `SQLITE_LIMIT_LENGTH`：
  `hex(randomblob(150000000))` 这类超时打不断的单次调用，在分配之前就被它拦下。
  库里已有的、比它更长的行读取时也会报错，要存更大的值，先调这个再存。
  PostgreSQL 没有单值长度的会话参数：那边的字节预算只管「返回多少」，不管驱动收到多少
  （用 `left()`，或让只读账号看不到那一列）。
- **SQLite 的只读执行器还装了数据库层授权器**：除读数据之外一律拒绝，
  `ATTACH`、`VACUUM INTO`、`PRAGMA`、`sqlite_*` / `pragma_*` / `dbstat` 即使应用层被绕过
  也会在引擎里被拒。
- **调大行数上限是有代价的。** 返回的每一行都会进 `/query` 响应和 A2A 的 data part，
  整份结果还会存进对话记忆、做摘要时交给摘要器。有理由再调，调完看一下响应大小和记忆库。
- 两个值都必须大于 0。`0` 会在启动时被拒绝，它不表示「不限」。
- 「数据浏览」走的是同一批执行器。上限比请求的每页行数还小时，一页按上限给，
  响应里的 `page_size` 是实际用的页大小。
- MCP stdio 服务（`python -m ai_sql_agent.mcp_server.server`，
  `PipelineConfig(use_mcp_protocol=True)` 走的也是它）不读这两个变量，固定 1000 行 / 10 秒。
- 在 SQLite 上验证过。PostgreSQL 上这两个值分别落到取数行数和 `statement_timeout`；
  这条分支只有用假驱动的单元测试，**没有在真实 PostgreSQL 上跑过**。

## 二·五、模型部署形态（公网 API / 内网 vLLM）

本项目支持两套协议：**OpenAI 兼容**（`POST /chat/completions` + `Authorization: Bearer`）
与 **Anthropic Messages**（`POST /v1/messages` + `x-api-key`），由 `AI_SQL_MODEL_PROVIDER`
选择；服务地址与模型名全部由配置项决定。因此模型跑在哪里、用哪家对应用代码是透明的，
换厂商或换部署形态**不需要改任何代码**，只改环境变量。
自建推理服务（vLLM / SGLang / Ollama）都走 OpenAI 兼容那一套。

### 两种形态对比

| | 公网 API | 内网自建 vLLM |
|---|---|---|
| 编排文件 | `deploy/docker-compose.prod.yml` | `deploy/docker-compose.vllm.yml` |
| 数据出网 | 问题原文与 Schema 会发给外部服务 | **数据不出内网** |
| 硬件成本 | 无 | 需要 GPU（14B bf16 约 32GB 显存） |
| 单次延迟 | 受公网与厂商排队影响 | 取决于本地 GPU，通常更稳定 |
| 模型能力 | 可用最新商业模型 | 受限于可私有化部署的开源模型 |
| 适用 | 数据敏感度低、不想维护 GPU | 客户信息、成本价等不允许出内网 |

选择依据是**数据合规要求**，不是性能。合同金额、客户与供应商信息、批次成本单价
这类数据如果不允许离开内网，就只能用自建形态。

### 切到内网 vLLM

```bash
# 1. 起 vLLM（OpenAI 兼容 server）
docker compose --project-directory . -f deploy/docker-compose.vllm.yml up -d

# 2. 或者对已有服务，只改这几个环境变量
AI_SQL_CHAT_URL=http://vllm-chat:8000/v1/chat/completions
AI_SQL_EMBEDDING_URL=http://vllm-embed:8000/v1/embeddings
AI_SQL_MODEL_PROVIDER=openai                  # vLLM 是 OpenAI 兼容 server
OPENAI_API_KEY=sk-internal-vllm               # vLLM --api-key 的值
AI_SQL_CHAT_MODEL=Qwen2.5-14B-Instruct        # 需与 --served-model-name 一致
AI_SQL_CODER_MODEL=Qwen2.5-Coder-14B-Instruct
AI_SQL_EMBEDDING_MODEL=bge-m3
AI_SQL_EMBEDDING_DIMENSIONS=1024
```

### 注意事项

- **模型名必须与 vLLM 的 `--served-model-name` 完全一致**，否则 vLLM 返回 404。
- **`--max-model-len` 要覆盖 Token 预算**。本项目单次查询预算约 8k，建议设 16384 留一倍余量。
- **Embedding 维度要和索引一致**。换 Embedding 模型后维度若变化，必须重建向量索引
  （`POST /admin/schema/reload`），否则维度不匹配会报错。
- **Rerank 不是 OpenAI 标准协议**，走的是厂商自定义格式，vLLM 不直接兼容。
  留空 `AI_SQL_RERANK_URL` 时字段级精排退化为本地词面覆盖度打分——可用，
  但弱于交叉编码精排；要恢复需自建 rerank 服务并加一层格式适配。
- **超时可以收紧**。本地推理没有公网抖动，`AI_SQL_HTTP_TIMEOUT_SECONDS` 从 60 降到 45 更合适。

### 兼容性验证

`tests/test_openai_compatible_endpoint.py` 起了一个最小 OpenAI 兼容服务作为被测端点，
验证四件事：配置可整体切换、chat 按 OpenAI 格式收发且鉴权头正确、embedding 同理、
以及**整条 Text2SQL 链路跑在自建端点上能产出可执行 SQL 与真实结果**。
改动模型接入相关代码后跑这个测试即可确认没有破坏兼容性。

```bash
python -m pytest tests/test_openai_compatible_endpoint.py -v
```

## 二·六、业务语义元数据：模型对业务的全部认知

接真实业务库时，这是工作量最大的一块，而且它不是编码工作——需要懂业务的人
把每张表每个字段是什么意思、业务上怎么称呼、几个库之间靠什么对应写下来。

服务自带四个演示库的元数据。换成真实库之后那份元数据就不适用了：检索退化成
按字段名字面匹配，「料快没了」再也匹配不到 `safety_stock`。现象是「模型答不准」，
实际是没人告诉过它业务是什么。

```bash
python -m ai_sql_agent.semantic export semantic/     # 导出演示元数据当模板
# 编辑 semantic/*.yaml：一个库一个文件，外加 _shared.yaml
python -m ai_sql_agent.semantic validate semantic/   # 与真实库比对结构
python -m ai_sql_agent.semantic show                 # 当前生效的是哪一份
```

每个库的文件里是表和字段的业务描述、别名、语义角色和样例值；`_shared.yaml`
放跨库的部分：关联键（这些系统之间没有外键，不显式声明规划就没法正确拆步）、
指标口径消歧（「采购金额」到底指哪个字段）、指标定义（「超预算」怎么算）。

`validate` 会连上真实库双向比对：

| 发现 | 后果 |
| --- | --- |
| 元数据里有、库里没有的表或字段 | 检索会把它推给模型，生成的 SQL 必定报错 |
| 库里有、元数据没写的表 | 对模型完全不存在，涉及它的问题永远答不出来 |
| 跨库关联键指向不存在的字段 | 跨库规划会静默失效 |
| 连接串直接写在文件里 | 文件要进版本库，密码不能——改用 `dsn_env` |

有错误时返回非 0，可以直接卡在发布流水线里。每次业务库改结构后都要重跑一次，
和 `POST /admin/schema/reload` 一起做。

生产部署把目录只读挂进去：

```yaml
volumes:
  - ./semantic:/app/semantic:ro
environment:
  AI_SQL_SEMANTIC_DIR: /app/semantic
```

### 同一份元数据也是页面「数据浏览」的来源

`GET /catalog` 把这份元数据与各库真实的表结构对在一起，再按调用方的权限裁剪，
让人在提问之前就能看到有哪些数据可问。元数据里登记了、库里没有的表显示为「库里没有」；
连不上的库显示为「不可用」——都不会显示成一张空清单。

给人看的名字与说明是可选的附加键，库、表、字段三层都认。它们**不会**发给模型，
检索读的仍然是 `description` 与 `aliases`。

```yaml
name: erp_db
label_en: Orders & Purchasing        # 或写 `label:`，所有语言共用一个名字
label_zh: 订单与采购
description_en: Orders, suppliers, purchasing and supplier payments.
tables:
  supplier:
    label_en: Suppliers
    columns:
      on_time_rate:
        description_en: Share of deliveries that arrived by the agreed date.
        business_usage_en: Find suppliers that deliver late.   # 字段 `business_usage` 的英文写法
        unit: ratio
```

共享文件里的业务术语和跨库关联也可以补同样的键。`metric_definitions_en` 的键就是
`metric_definitions` 里的指标名——口径仍然只有一份，这里是它的英文写法：

```yaml
# _shared.yaml
metric_definitions:
  准时率: 准时率是供应商按约定交期到货的批次占比。
metric_definitions_en:
  准时率:
    term: On-time rate
    definition: Share of a supplier's deliveries that arrived by the agreed date.
cross_database_links:
  - left: erp_db.supplier.supplier_id
    right: wms_db.batch.supplier_id
    meaning: ERP 供应商编号与入库批次的供应商编号同域
    meaning_en: Same set of suppliers in both systems.
```

没写时页面依次退回：当前语言的第一个别名 → 把技术名拆开；库标识符是常见系统类型的
（`erp_db`、`pm_db`、`mes_db`、`wms_db`）有一个默认的业务名。只有另一种语言的说明时
照原文显示，不做翻译。

「业务查数」首页的建议问题也是拿这份目录拼的，页面自己不带任何一句问题：角色是
`filter` 或 `dimension`、并且 `value_range` 把取值写成清单（`待处理 / 处理中 / 已解决`）
的字段，拼出「按它统计数量」和「筛出某个取值」；数值型的 `metric` 拼出「最高的 10 条」
和「按某字段看平均值」；`time` 字段拼出「按月统计」——用的都是上面写的表名和字段名。
不拼「合计」：数据字典没说一个指标能不能相加，比率和单价加起来没有意义。
字段没有标角色的表拼不出建议，读不到的库不出现。建议只是放进输入框的一句话，
改不改、发不发由人定。目录关掉之后没有建议，提问照常可用。

只读预览的上限由 `AI_SQL_CATALOG_*` 控制（见 `.env.example`）；
`AI_SQL_CATALOG_ENABLED=false` 可以只关掉「数据浏览」，不影响提问——
页面从 `GET /whoami` 取调用方身份，这个接口不受开关影响。

预览做什么、不做什么：

- 浏览有自己的限流桶（`AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE`），翻表不占提问的额度。
  Key 自己的 `rate_limit_per_minute` 更低时，按更低的那个算。
- 取回的值在 SQL 里就设了上限：文本截到 2000 个字符（响应里会标明哪次截过），
  二进制字段只取大小。筛选和排序仍然按完整的值算。
- 业务术语和表一样按权限裁剪：定义里提到了调用方看不到的字段，整条不给。
- 「数据浏览」页上的统计图来自 `POST /catalog/{database}/{table}/profile`：总行数、
  各字段填了多少、取值分布、按月的记录数。它只返回聚合结果，能统计的就是预览里看得到
  的那些字段，搜索词与筛选条件和预览同义，限流也算在浏览的桶里。一个字段的分布不受它
  自己的筛选条件影响，所以选了一个取值之后还能换成另一个。每一项聚合都是一遍全表扫描，
  单条受 `AI_SQL_CATALOG_PREVIEW_TIMEOUT_SECONDS` 限制，一次请求累计超过它的两倍就不再
  往下跑；没算完的分布在响应里记为跳过，不拿空结果冒充。结果按调用方与用户缓存 15 秒。
  `AI_SQL_CATALOG_PROFILE_ENABLED=false` 只关统计，目录和预览不受影响。
- 只读安全校验不放行的表名、字段名（它按整条语句扫写操作关键字，引号里的也算——
  `update`、`Last Update`、`set`）在目录里照常列出，但标成「预览不了」。
  提问涉及这样的字段时，同样会被这道校验拒掉。
- 库文件不存在，或者存在但是 0 字节，都显示为「不可用」。读表结构不再在文件缺失时
  顺手把它建出来。

## 二·七、反馈回路：让工具越用越准

页面上每条回答下面都有「这条回答对吗」。这个判定是唯一不依赖我们自己出题的
数据来源。

- **答错**进待改进清单。绝大多数答错都能追溯到元数据缺失，而用户那句补充说明
  往往直接点出缺的是哪一条；
- **答对**成为认证范例：下次遇到相似问题，它会作为参考写法进生成 Prompt，
  把这家公司的 join 路径和口径固化下来，而不是每次重新猜。

```bash
python -m ai_sql_agent.feedback stats                    # 线上准确率与静默错误率
python -m ai_sql_agent.feedback wrong                    # 待处理清单，带用户说明
python -m ai_sql_agent.feedback export-dataset new.json  # 把答错的问题导成评测集草稿
python -m ai_sql_agent.feedback resolve <trace_id>       # 标记某条已处理
```

`export-dataset` 导出的是草稿：问题原文、模型答错的 SQL、用户的说明都摆好了，
但正确 SQL 得人来补——模型答错了，系统里没有任何地方知道正确答案。补完并进
`benchmark/dataset.json` 再重跑门禁，真实用户答错过的题就成了以后不能悄悄
退化回去的红线。

运维上主要看两个数（都来自 `feedback stats`）：

| 指标 | 怎么读 |
| --- | --- |
| 线上准确率 | 有明确判定的样本里答对的占比。与 benchmark 对照，两边差得远时以这个为准 |
| 线上静默错误率 | 答错且当时没有任何风险提示的占比。最贵的一类——用户没有任何理由去核对 |

反馈按人隔离：只能对自己的查询表态，认证范例也只回给确认它的那个人——
一条查询里带着表名，等于绕过权限层看 Schema。

## 三、权限配置

`api_keys.json` 里的 `policies` 决定每个角色能看哪些表。生效在两处：
检索阶段（无权表不会进入 Prompt，模型看不到）与执行阶段
（SQL 里引用了无权表直接拒绝，不落到数据库）。

```json
"policies": {
  "analyst": {
    "allow_tables": ["erp_db.*", "pm_db.*", "mes_db.*", "wms_db.*"],
    "deny_columns": ["*.*.id_card_no"]
  },
  "hr": { "allow_tables": ["hr_db.*"] }
}
```

- `deny` 优先于 `allow`；
- 一个 Key 配多个角色时权限取并集；
- `can_impersonate: true` 只给服务端调用方（如 BI 门户），
  个人 Key 不要开——否则改一下请求体的 `user_id` 就能读到别人的个人知识库。

## 四、探针

| 端点 | 用途 | 编排配置 |
| --- | --- | --- |
| `/health` | 存活：只看进程，不查依赖 | livenessProbe |
| `/readyz` | 就绪：查记忆库、审计库、pgvector | readinessProbe |
| `/metrics` | Prometheus 抓取 | ServiceMonitor / scrape_config |
| `/` | 查询界面：业务查数、数据浏览、历史查询（静态文件在 `/static` 下，无构建步骤） | 内网直接访问 |

依赖抖动时 `/readyz` 返回 503 摘掉流量，但 `/health` 仍然 200，
避免编排系统把进程反复重启。

Kubernetes 片段：

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 15
  periodSeconds: 20
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
```

副本扩容优先加 replica，不要加 uvicorn worker：
Pipeline 与 Schema 索引在进程内共享，多 worker 会各建一份。

## 五、监控指标

`GET /metrics` 暴露（Prometheus 文本格式，零外部依赖自实现）：

| 指标 | 类型 | 用途 |
| --- | --- | --- |
| `ai_sql_requests_total{method,path,status}` | counter | QPS、错误率 |
| `ai_sql_request_duration_seconds` | histogram | P95/P99 延迟 |
| `ai_sql_stage_duration_seconds{stage,status}` | histogram | 哪一段慢（检索/规划/生成/执行/修复） |
| `ai_sql_model_calls_total{component,status,error_category}` | counter | 模型失败率、限流次数 |
| `ai_sql_model_tokens_total{component,kind}` | counter | Token 消耗与成本 |
| `ai_sql_sql_executions_total{database,status}` | counter | SQL 成功率、越权次数 |
| `ai_sql_sql_repairs_total{result}` | counter | 修复回路的实际收益 |
| `ai_sql_access_denied_total{database}` | counter | 数据权限拦截 |
| `ai_sql_limit_rejections_total{reason}` | counter | 限流/配额拒绝 |
| `ai_sql_degraded_responses_total{reason,failed_stage}` | counter | 模型不可用触发的降级 |
| `ai_sql_schema_reloads_total{result}` | counter | Schema 索引重建 |
| `ai_sql_circuit_state{component}` | gauge | 熔断状态 0=闭合 1=半开 2=断开 |
| `ai_sql_circuit_trips_total{component}` | counter | 熔断跳闸次数 |
| `ai_sql_runs_total{entry,status}` | counter | 结束的运行，按入口（`http_query` / `a2a_send` / `a2a_stream`）与状态（`completed` / `failed` / `cancelled` / `interrupted`） |
| `ai_sql_run_cancellations_total{reason}` | counter | 取消请求：`api`、`client_disconnected`、`deadline`、`shutdown` |
| `ai_sql_run_cancel_latency_seconds{reason}` | histogram | 从收到取消到真正停下的耗时，即检查点的实际粒度 |
| `ai_sql_run_events_dropped_total` | counter | 没能落库的事件数，查询本身不受影响 |

常用 PromQL：

```promql
# P95 查询延迟
histogram_quantile(0.95, sum(rate(ai_sql_request_duration_seconds_bucket{path="/query"}[5m])) by (le))

# SQL 执行成功率
sum(rate(ai_sql_sql_executions_total{status="ok"}[5m]))
  / sum(rate(ai_sql_sql_executions_total[5m]))

# 修复回路救回来的比例
sum(rate(ai_sql_sql_repairs_total{result="repaired"}[1h]))
  / sum(rate(ai_sql_sql_repairs_total[1h]))

# 今日 Token 消耗
sum(increase(ai_sql_model_tokens_total[24h])) by (component)
```

建议告警：模型失败率 > 5%、`/query` P95 > 10s、`ai_sql_limit_rejections_total`
突增（说明配额设小了或有人写了循环）、`/readyz` 连续失败、
`ai_sql_circuit_state > 0` 持续 5 分钟（模型侧真的挂了）。

## 六、排查一次失败查询

每个响应都带 `trace_id`（响应体字段与 `X-Trace-Id` 响应头），日志与审计都按它检索：

```bash
# 1. 从日志捞全链路（JSON 格式下用 jq）
docker compose --project-directory . -f deploy/docker-compose.prod.yml logs app | grep <trace_id>

# 2. 从审计表看这次查询做了什么
docker compose --project-directory . -f deploy/docker-compose.prod.yml exec postgres \
  psql -U ai_sql -d ai_sql -c \
  "SELECT created_at, caller, user_id, query, route, databases, sqls, success, error
     FROM ai_sql_audit_log WHERE trace_id = '<trace_id>'"
```

日志里能看到每一段：`stage.retrieval`（命中哪些库表）、`stage.planning`
（拆了几步）、`stage.sql_generation`（生成的 SQL）、`stage.execution`
（成功与否、行数）、`repair.attempt`（修复过程）、`model.call`（模型耗时与重试）。

不想翻日志的话，可以直接看这次运行的事件日志，所有事件按顺序排好。审计行的 `run_id` 列、响应头 `X-Run-Id`、
A2A 的 taskId，都能找到它：

```bash
curl -s http://localhost:8000/runs/<run_id>/events -H 'X-API-Key: …' | jq '.events[] | [.seq, .type, .stage, .step]'
```

事件类型有：`run.started`、`stage.started` / `stage.finished`、`model.call`、`tool.sql`、
`run.cancel_requested`、`run.finished`。事件里不存提示词、模型原始输出和结果行；SQL 文本最多保留 8 KiB。

## 七、常用运维查询

```sql
-- 今天各用户用量（配额判断用的就是这张表）
SELECT user_id, COUNT(*) AS queries, SUM(prompt_tokens + completion_tokens) AS tokens
FROM ai_sql_audit_log
WHERE created_at >= date_trunc('day', now())
GROUP BY user_id ORDER BY tokens DESC;

-- 最近失败的查询
SELECT created_at, user_id, query, error
FROM ai_sql_audit_log
WHERE NOT success ORDER BY id DESC LIMIT 20;

-- 越权访问尝试（谁在试图查没权限的表）
SELECT created_at, caller, user_id, query, denied_tables
FROM ai_sql_audit_log
WHERE jsonb_array_length(denied_tables) > 0 ORDER BY id DESC;

-- 高频问题（值得沉淀成固定报表的）
SELECT query, COUNT(*) FROM ai_sql_audit_log
WHERE created_at > now() - interval '7 days'
GROUP BY query ORDER BY 2 DESC LIMIT 20;
```

## 八、Schema 变更后

设了 `AI_SQL_SCHEMA_REFRESH_SECONDS` 就不用管：后台按周期比对指纹，
变了自动重建（日志 `schema_watcher.change_detected` → `schema.reloaded`）。

不想等下一个周期，或想确认当前状态：

```bash
curl -H "X-API-Key: $ADMIN_KEY" localhost:8000/admin/schema/status
curl -XPOST -H "X-API-Key: $ADMIN_KEY" localhost:8000/admin/schema/reload
```

这两个接口要求 Key 带 `"is_admin": true`。彻底强制重算向量：

```sql
DELETE FROM ai_sql_vector_meta;
```

## 八之二、模型不可用时会发生什么

不会返回 5xx。规划 / 生成 / 修复阶段模型失败时，系统会取该用户同一问题上次
成功的 SQL 重放（数据实时查、权限重新校验）；没有可重放的历史就返回
明确的降级说明。响应里的 `degraded` 与审计表的 `degraded` 列都能查到。

如果只是语义自检没做成（查询本身成功了），结果照常返回，但带一条
`self_check_not_run` 的风险提示，`has_risk` 为真——没核对过的结果不会被报成核对通过。

```promql
# 降级率突增说明模型侧有问题
sum(rate(ai_sql_degraded_responses_total[5m])) by (reason, failed_stage)
```

```sql
-- 最近发生过降级的查询
SELECT created_at, user_id, query, degraded FROM ai_sql_audit_log
WHERE degraded ORDER BY id DESC LIMIT 20;
```

### 凭证本身被拒的时候

Key 被拒和服务抖动不是一回事：重试、或者让下一个阶段再去试，只是多发几次注定失败的请求。
模型调用的统一入口会记住它，按**服务地址 + 凭证**记（Key 只以带盐的指纹参与比较，
不存明文，也不进日志）：

| 服务端说 | 停掉什么 | 到什么时候 |
| --- | --- | --- |
| 401；或 403 且写明 Key 无效 / 停用 | 这把 Key 对这个服务的所有调用 | 重启，或管理员复位 |
| 402；或响应体写明额度 / 余额用尽 | 同上 | 重启，或管理员复位 |
| 403 且写明这把 Key 无权使用该模型 | 只停这个模型 | 重启，或管理员复位 |
| 429 且 `Retry-After` 超过单次请求里等得起的上限 | 这个模型，暂缓 `Retry-After` 那么久（封顶 5 分钟） | 到点自动恢复 |
| 没说原因的 403 | 这个模型，暂缓 60 秒 | 到点自动恢复 |

普通的 429 仍然在原地重试，次数有上限，等多久照 `Retry-After` 来。
别的 Key、别的服务、别的模型不受影响。

Key 被确认拒绝之后，一个问题不再产生任何模型调用（以前是两三次），返回的仍是平常的降级答复。
`GET /readyz` 在 `model_access` 下列出已知故障，`model_credentials` 不再写 ok；
它不会为此去探测模型。恢复方式：修好 Key 或额度后重启；不想重启时调用已有的管理员接口
`POST /admin/circuit/reset`（需要管理员 Key），它会一并清掉这些状态。不会自动重试；
复位也不验证 Key 是否真的好了——下一个真实请求自己去试。在页面上换一把 Key（见下一节）
也会清掉这些状态。

它做不到的事：第一个拒绝回来之前已经发出去的请求收不回来。N 个并发请求时，
最多会有 N 次被拒的调用发出，之后才全部停住。

### 从页面修改模型连接

管理员可以在 **设置 → 高级设置 → 模型连接** 里换掉主链路的模型连接——协议、接口地址、Key、
规划模型、SQL 模型——不用重启。接口：`GET /admin/model`（现状，永远不含 Key）、
`POST /admin/model/test`、`PUT /admin/model`、`DELETE /admin/model`（恢复）。

- **谁能改。** `AI_SQL_AUTH_MODE=api_key` 时要管理员 Key，和其他 `/admin` 接口一样。
  没开鉴权时只接受**本机直接发来**的请求：对端是回环地址、`Host` 是本机名字、没有代理转发的头、
  浏览器请求必须同源；其他人只看得到一份只读的摘要。同一台机器上的反向代理如果把 `Host`
  改写成回环地址、又不加任何转发头，这里分辨不出来：要把实例暴露出去，先开鉴权，或者关掉这个功能。
  跑在容器里时，宿主机浏览器的请求来自容器网桥而不是回环地址——这种部署用管理员 Key。
- **改的是什么。** 页面保存的连接压过环境变量和 `.env`，直到有人点「恢复为服务端配置」。
  Chat 端点一律按「保存的接口地址 + 标准路径」重新拼，服务端配的 `AI_SQL_CHAT_URL` 不沿用。
  Rerank 继续用服务端的凭证和端点；Embedding 单独一节（见下文）。
- **Key 只进不出。** 任何响应、日志、报错里都不带它；状态里只有「配没配」和末四位。
  一把 Key 只发给它被填进来时对应的那台主机——接口地址换到别的主机就要重新填 Key（两种协议各用各的 Key）。
  它和 `.env` 一样以明文存在 `AI_SQL_MODEL_SETTINGS_PATH` 指的文件里（权限 0600；
  compose 部署时这个文件在 `ai_sql_runtime` 卷上）。
- **测试连接**会真的调用模型：每个模型名一条极短的请求。它什么都不保存，也不会把正在用的连接
  记成「已被拒绝」。保存本身不做测试；页面会在保存后自动测一次。
- **生效时**清掉熔断器和已记下的拒绝，并重建启动时按「有没有凭证」选过一次的部分
  （意图路由、数据问答、对话摘要、关键词抽取）。已经在处理的问题用旧连接跑完。
- **留痕。** 每次修改记一条 `model_settings.saved` / `model_settings.cleared`（谁、协议、
  接口主机、模型名、Key 有没有换——不记 Key 本身）。用着页面保存的连接时，`/readyz` 里有
  `model_settings`，启动日志写 `model_settings=page`。保存的文件读不出来时也在这两处说明，并按没有处理。
- **多副本**各自有一份文件，一次保存只到得了其中一个。这种部署请设
  `AI_SQL_MODEL_SETTINGS_ENABLED=false`，继续用环境变量配置。

#### 表结构检索用的 Embedding

表结构检索按关键词和向量两路召回库、表、字段。向量默认是本地哈希（`AI_SQL_EMBEDDING_BACKEND=local`）：
不调任何服务，但没有语义，口语问法要靠关键词和抽词时的术语映射。设成 `remote` 就改用 OpenAI 兼容的
`/embeddings`（`AI_SQL_EMBEDDING_URL`、`_MODEL`、`_DIMENSIONS`、`_API_KEY`，Key 没配时回落到
`OPENAI_API_KEY`）。也可以在 **设置 → 高级设置 → Embedding** 里配，对应
`POST /admin/model/embedding/test`、`PUT /admin/model/embedding`、`DELETE /admin/model/embedding`。

- **保存时先重建。** 用新模型把所有表和字段的说明算一遍向量，整个索引建成了才换上、才写文件。
  测试没通过（HTTP 422）、重建失败（HTTP 502）、文件写不了，正在用的索引和保存的文件都不动。
  恢复为服务端配置时同样按服务端配置重建一遍。
- **维度**默认 1024：OpenAI 的 `text-embedding-3-*` 和百炼的 `text-embedding-v4` 都能按请求输出这个维度，
  也和已有的 pgvector 表一致。服务按别的维度返回时测试不通过（`dimension_mismatch`）。
- **Key。** Embedding 这一节存自己的 Key。留空时沿用同一台主机上已经配好的那把——Embedding 现在用的，
  或者两边都指向同一个 OpenAI 主机时对话模型那把；换了主机必须重填。
- **服务出问题时。** 启动或重建 Schema 时远端 Embedding 建不起索引，就退回本地向量，服务照常起，
  `/admin/model` 的 `embedding.fallback` 和日志 `schema.embedding_fallback` 说明原因。答题过程中，
  一个问题的全部关键词合成一次请求并缓存；这次请求失败时，这个问题只走关键词召回，
  之后 30 秒内不再去调这个服务（`retrieval.embedding_unavailable`）。
- **哪些内容会发出去。** 建索引时是表和字段的说明（不含样例值），答题时是每个问题的关键词。

#### OpenAI 的推理模型

gpt-5 起和 o 系列的模型，请求不带温度（这些模型只认默认值），输出上限用 `max_completion_tokens`
（`AI_SQL_MAX_COMPLETION_TOKENS`，默认 8192，推理过程也算在里面），并带 `reasoning_effort`
（`AI_SQL_REASONING_EFFORT`，默认 `low`；留空则不传）。服务端点名拒绝其中某个参数时，去掉它重发一次，
并记住这个端点上的这个模型。在 OpenAI 兼容端点上，意图路由要求按严格的 JSON Schema 输出。
输出到了长度上限（`finish_reason=length`）按调用失败处理，不当成完整的规划或 SQL 去解析。

## 八之三、业务库只读账号

只读的第三层防线是数据库账号本身。配错了平时看不出来，
所以服务启动会自动体检并在日志里喊（`readonly_audit.violation`），
`/readyz` 的 `business_db_accounts` 也能看到。

```bash
# 建最小权限只读角色
python -m ai_sql_agent.ops provision \
  --admin-dsn "postgresql://postgres:***@db:5432/yourdb" \
  --database yourdb --host db

# 体检（只查系统目录，不写数据，退出码非 0 表示不合格，可以进 CI）
python -m ai_sql_agent.ops audit --dsn "$AI_SQL_BUSINESS_DSN"
```

角色自带 `statement_timeout`、`idle_in_transaction_session_timeout`
和 `default_transaction_read_only = on`：应用层配置失效时数据库仍然兜底。

## 九、备份

需要备份的是 PostgreSQL 一个库（Schema 向量、长期记忆向量、审计）
加上容器卷里的 `runtime_data`（记忆原文 SQLite）：

```bash
docker compose --project-directory . -f deploy/docker-compose.prod.yml exec postgres \
  pg_dump -U ai_sql ai_sql | gzip > ai_sql_$(date +%F).sql.gz
```

记忆库默认是 API 启动目录下的 `runtime_data/memory.db`，`AI_SQL_MEMORY_DB_PATH` 可以改。
同一台机器上起两个实例时要各配各的路径：共用一个文件的话，在一个实例里问过的问题，
会被另一个实例当成「刚才的查询结果」来回答。配置指向的文件按原样打开——改配置不会
搬动或清空任何东西，所以把实例指到新路径等于从空的历史开始，旧文件留在原处。
