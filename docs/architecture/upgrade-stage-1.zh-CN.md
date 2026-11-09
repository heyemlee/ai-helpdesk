# 阶段 1：模型协议与真实端点基线

日期：2026-11-05（America/Los_Angeles）。接续 `c58c6dc` 的初版 Responses 适配与 `a9b4cd4` 的协调记录。本阶段只实施协议、连接设置、诊断与验收；没有部署、修改正在使用的模型配置或操作业务数据。

## 交付与接口

- Helpdesk 的 `OPENAI_API_MODE=chat|responses` 与页面保存的 `chat.values.api_mode` 贯通。页面连接优先；旧配置缺字段明确按 `chat` 解释，不被环境中的 `responses` 悄悄改变。协议或路径变化会触发运行时换客户端。
- SQL Agent 使用独立的 `AI_SQL_OPENAI_API_MODE`（兼容 `OPENAI_API_MODE`），页面请求字段为 `api_mode`，存储字段为 `openai_api_mode`。设置覆盖、端点拼接、规划、生成、抽词客户端及诊断均使用选定协议。显式 `AI_SQL_CHAT_URL` 仍有优先权，需要与协议一致。Embedding/Rerank 保持独立配置，不继承生成模型协议。
- 两个设置页面均可显式选择 Chat Completions / Responses；只改协议也能保存。旧协议默认值和生产保存配置均未改动。目标模型须明确填 `gpt-6.1-sol`，不会按模型名猜第三方服务能力，也不会自动切回 Chat。
- Helpdesk Responses 保留原始 message/function_call/reasoning 项及 call_id，工具错误参数按原样回传，支持并行工具与后续轮次。推理项只供当前调用链重放；事件日志只记类型，不记录加密内容或隐藏推理正文。跨 Run 的模型重放不是本阶段交付。
- 旧 Chat 模式的 Schema 降级缓存与 Responses 诊断隔离，同地址/同模型切换协议不会继续显示旧降级警告。
- 结构化输出发送 `text.format` 的严格 JSON Schema，先验证终态再本地 Pydantic 校验。Responses 不采用 Chat 的提示词降级或参数删除重试。
- 空输出、仅有推理的空正文、拒答、未知/失败/取消状态、截断、内层未完成的 message/function_call/reasoning、流缺终态或终态矛盾均报错。截断的工具调用不会执行，半截 SQL 不会交给执行器。
- 流以最终 response 输出为权威，不使用 `output_item.added` 中可能尚未完成的推理项。完成后的多次读取不会重放流。拒答及失败不是正常完成的答案。
- 保留原有超时、SDK/SQL 重试预算、准入控制、429/凭据保护。已返回用量的失败响应也记账；总输入/输出、缓存读取和推理子计数保留，子计数不会重复加到总 token。没返回用量的断流保持未知。
- Helpdesk Responses 连接诊断最多五条合成请求：文本、结构化输出、工具调用、工具回传、流；前项失败后剩余项标记跳过。SQL 诊断按所选协议发送，并实际验证非空、完整正文，不再以“200 且看起来像 JSON”判成功。SQL 的独立链路只消费文本/结构化 SQL，不执行模型 function tools。

## 官方资料核对

目标保持 `gpt-6.1-sol`。官方模型页说明其工具调用使用 Responses，支持结构化输出和流；推理 effort 为 low/medium/high/xhigh/max，不支持 none。请求采用 `max_output_tokens`，有推理时不带 temperature/top_p。参见 [Sol 模型页](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 与 [GPT-6 迁移说明](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.1-sol)。

当前参考文档说明加密推理内容默认返回；兼容写法 `include=["reasoning.encrypted_content"]` 仍受支持。本适配保留显式 include；没有把“未写 include”本身视为协议失败。流式推理要使用已完成的项，不能重放 added 事件中的半成品。参见 [Responses 创建参数](https://developers.openai.com/api/reference/resources/responses/methods/create) 与 [流事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)。

## 自动验收

实际使用仓库中受版本控制的 `.claude/skills/test-backend/` 隔离入口，复制 git 可见文件并清空继承环境，契约取自同一副本的 `a2a-contract/`。本机 `.agents` 技能副本及其 `.Codex` 示例路径已过期，未作为验收入口、未提交它们。

| 门禁 | 命令/范围 | 最终结果 |
| --- | --- | --- |
| Helpdesk + 当前契约 | `.claude/skills/test-backend/scripts/run.sh --full` | ruff、契约自检通过；1,755 passed，0 skipped |
| SQL Agent + 当前契约 | `.claude/skills/test-backend/scripts/run-sql-agent.sh --full` | unittest 1,239 passed；迁移 pytest 6 passed；均 0 skipped；契约子集复查 41 passed |
| 前端 | 完整仓库临时副本，复用本机 lock 对应依赖，空环境运行 `npm test`、`npm run build` | 399 passed，0 skipped；TypeScript 与 Vite 构建通过 |

全量通过后，针对 Chat→Responses Schema 缓存隔离补充了一个反例，最终对 `test_openai_compat.py`、`test_openai_responses.py`、`test_model_connection.py` 再跑隔离回归：**131 passed，0 skipped**，ruff 与当前契约自检同时通过。该组与全量重叠，不累加为新总数。

后端使用一次性 pgvector/Redis/Kafka，SQL 使用独立一次性 pgvector，均随机回环端口；没有连接生产 PG 5433 或 Redis 6379。SQL gate 的 41 条契约/业务结果复查是全量 unittest 的子集，不重复计入总数。前端没有运行新的 `npm ci`，也没有浏览器端到端或镜像构建；组件自动流程覆盖协议单独改变时测试与保存两端收到相同值。构建保留已有 bundle >500 kB 提示；SQL 测试日志有连接资源警告，不是跳过或断言失败。

新增自动验收覆盖真实 SDK 的 HTTP/SSE 解码、三轮推理/工具回传、错误参数、并行调用、完整工具循环、图片输入转换、Schema、拒答/空输出/各类未完成状态、超时/429/400 无协议回退、失败用量、设置持久化与运行时切换、诊断跳过以及 Embedding 协议隔离。这里的 HTTP 服务均为离线替身，只证明协议与故障处理，不代表模型质量。

## 真实端点证据：6 / 6 请求

安全读取已有页面配置后确认：Helpdesk 与 SQL Agent 使用同一份第三方 HTTPS 地址和凭据。连接当时保存为 Chat + Luna；探测通过单独进程指定 Responses + `gpt-6.1-sol`，没有保存配置。地址按类别记录，不记录主机、凭据或业务内容。

固定数据版本 `protocol-synthetic-v1`；effort=low；max_output_tokens=256；timeout=25s；max_retries=0；store=false。共六个请求，无重试、无列模型请求、无自动协议回退，之后没有再发真实请求。

| 实际调用 | 结果 | 输入 / 输出 token | 延迟 ms |
| --- | --- | --- | --- |
| Helpdesk 文本 | OK，返回模型字段匹配目标 | 4,226 / 5 | 4,513 |
| SQL 独立传输文本 | OK，请求指定目标模型 | 4,226 / 5 | 2,888 |
| Helpdesk 严格 Schema | value=7 且本地校验通过 | 4,258 / 13 | 2,514 |
| Helpdesk 工具调用 | 指定合成工具、参数 value=7 | 4,277 / 34 | 4,127 |
| Helpdesk 工具回传 | 按同一 call_id 回传，最终文本 7 | 4,323 / 5 | 3,301 |
| Helpdesk SSE | 有文本增量、完整终态与用量 | 4,226 / 5 | 3,275 |

工具响应中观察到一个加密推理项，并完整带到回传请求。五个 Helpdesk 响应的 model 字段均为 `gpt-6.1-sol`；SQL 传输返回纯文本，未独立保留响应 model 字段。第三方回报不能独立证明其背后的实际模型身份，也不能证明所有 Schema 情形都由服务端强制执行。

服务报告合计输入 **25,536**、输出 **67** token。Helpdesk 部分报告缓存读取 8,192 token；当次 SQL 汇总尚未记录缓存/推理细分，不能把缺失细分填成零。短提示仍报告约 4.2k 输入 token，未探究服务方计量来源；无账单核对，不据此声称真实费用或官方费率。这六个单样本延迟不是 p50/p95。

原始脱敏结果：[JSON](evidence/stage-1-endpoint-20261105.json)。执行时脚本快照：[文本快照](evidence/stage-1-probe-harness-20261105.py.txt)，SHA-256 与结果中的 `harness_sha256` 一致。可维护入口：[probe_model_protocol.py](../../backend/scripts/probe_model_protocol.py)；当前入口与执行快照仅 import 排序不同，测试问题和请求预算相同。

探测执行时 HEAD 为 `a9b4cd4`，有本阶段未提交改动。随后补充了更严格的错误状态、诊断、配置与用量处理，因此这份证据不是“最终提交原样跑过六次”的声明。最终提交提供下一阶段同 Sol/同协议回归的候选锚点；阶段 6 应在该锚点固定数据与配置重跑业务质量基线。

可复现命令（会付费；必须另获真实调用预算后使用，本阶段额度已经用完）：

```sh
env -i PATH="$PATH" HOME="$HOME" backend/.venv/bin/python backend/scripts/probe_model_protocol.py \
  --backend-settings "$PWD/backend/data/runtime/model_settings.json" \
  --sql-env "$PWD/deploy/sql-agent/sql-agent.env" \
  --sql-container ai-helpdesk-sql-agent \
  --model gpt-6.1-sol --output /tmp/stage-1-probe.json --execute
```

## 交接与未完成项

1. **阶段 1 协议实现与有限端点能力已完成；未上线。** 生产仍使用原保存配置。上线变更由后续阶段执行。
2. 没有同 Sol、同配置、同业务数据的升级前后质量分数；历史 Luna 成绩保留原口径，不作为本次代码收益。六请求探测不替代阶段 6 的 200 场景、三轮候选评测、负载与费用门禁。
3. 真实多工具并发、超过一轮的工具链、真实拒答/截断/429/超时故障及图片理解未额外调用；这些协议故障和多轮项有离线自动覆盖，图片理解与授权链路交阶段 3。
4. 前端浏览器端到端、镜像构建、发布/回滚、业务质量及完整调用链成本仍属后续验收，不能把本阶段组件测试和协议探测算成这些项目通过。
5. 下一阶段可以沿用统一 `client.messages` 调用接口与 SQL `ModelTransport.chat/chat_stream`；新的 Responses 异常会中断当前输出，调用方应保持“失败/等待补料/人工处理”语义，不能将半截结果当作业务已完成。已有工具预算、去重、租约等机制未重写。

## 提交

- `4adc69e` — `fix(llm): complete explicit Responses protocols and diagnostics`：协议实现、设置与诊断、前端、自动回归和有限探测入口。
- 本报告与脱敏证据另以文档提交交付；本阶段末提交是后续检出的交付锚点。协议实现锚点为 `4adc69e`，最终回复给出两笔完整交付的短哈希。

阶段 2 可在主对话验收后继续；本阶段不再占用代码实施窗口。
