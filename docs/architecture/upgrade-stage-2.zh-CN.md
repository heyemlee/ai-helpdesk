# 阶段 2：跨消息 Case 与 Run

实施基线：`0ad098f`。实现提交：`14bfa9f`（`feat(cases): add versioned cross-message ticket workflow`）。验收日期：2026-11-05（America/Los_Angeles）。本阶段未部署，未修改运行配置、生产数据或生产容器，未调用真实模型。

## 已完成范围

复用 `tickets.id` 作为业务问题的唯一身份，新增 `ticket_case_state` 保存唯一的完整状态；原 `tickets.status` 是旧应用可读的兼容投影。没有另造一套独立 Case 编号或竞争状态。Conversation 表示沟通，Case 表示一个业务问题，Run 表示一次执行；同一会话、同一订单可以有多个明确选择的问题，普通 FAQ 沿用原路径，不强制建 Case。

工单入口增加“进入跨消息工单”，贯通以下流程：

1. 明确选择现有问题或新建问题，提交业务客户引用、问题类型、订单引用、文字和明确材料。
2. 服务端保存消息与材料，返回稳定的消息回执和 Run；缺材料时进入 `waiting_for_info`，本次 Run 完成并释放 Worker。
3. 在同一问题补料，以新的材料版本启动新 Run，继续查库、检索和形成草稿。
4. 人工接管、交回助手、解决、取消、填写原因后重开。接管独立于生命周期，接管/交回递增控制权版本。
5. 查看当前事实、待补项、附件引用、消息、执行结果、历史事件及草稿依据；审批指定版本、正文哈希和控制权版本的草稿。
6. 材料或控制权变化使旧草稿失效。版本冲突保留前端输入，刷新后由员工核对再提交；网络结果不确定时重试原消息 ID 和原请求体。

确认草稿仅记录内部审批，没有发送客户消息的操作。

## 服务端必填规则与事实边界

| 问题类型 | 服务端必需材料 |
| --- | --- |
| general | 不增加固定业务必填项 |
| order | 订单引用 |
| missing_parts | 订单引用、缺件型号、缺件数量 |
| wrong_item | 订单引用、应收型号、实收型号 |

`pending_fields=[]` 不能绕过这些规则。额外待补字段可由员工明确指定；明确提交的材料和可信核验证据都会重算待补项。这里的规则检查材料是否提供，不宣称已经完成 ERP 核验、图片识别、数量单位核对或赔偿判定。

`facts` 区分 `employee_material`（员工明确提供）与 `verified_evidence`（可信服务核实后采纳）。模型自由文本不会自动写入事实、负责人、状态指令或版本。草稿引用事实来源消息、证据编号/附件哈希、知识引用、业务查询记录和工具轨迹。政策适用范围和结构化业务字段的进一步核验属于阶段 4。

Case 执行使用数据库保存的当前问题快照与本问题历史，不读取同一会话里其他问题的 Redis 记忆。后续短句查询会带上本 Case 的订单引用和问题类型；SQL Agent 会话维度为 `case-<instance>-<case_id>`，调用员工身份取自服务端 Actor，而不是浏览器 `user_id`。既有 SQL Agent 只读、部署范围和 impersonation preflight 未放宽。

## 原子提交与并发保护

实现位于 `backend/app/tickets/cases.py`、`case_execution.py`、`case_schema.py`。

- 输入事务同时保存 Case 材料版本、消息审计、消息/Case/Run 关联、附件关联和待执行 Run。按稳定 `message_id` 持久去重；内容冲突返回 409。重复附件关联不额外增加材料版本。显式清空引用与省略字段的幂等指纹不同。
- Run 保存 `expected_case_version`、`expected_control_version`、可信 Actor 和材料快照。锁顺序统一为 Run（执行提交时）→ `tickets` → Case 状态 → 草稿/业务动作，旧 writer 也遵循父工单在前。
- 结果事务锁定 Run 和 Case，验证材料版本、控制权版本、生命周期、人工控制及 Run owner/attempt/租约。草稿、业务动作、outbox、助手消息、业务回执和 Run 终态一起提交。
- 最后的 Run 条件更新再次检查数据库时钟下租约仍有效。若等待 Case 锁期间租约已过期，整笔事务回滚，已经准备写入的草稿、通知和历史也不会落库。
- 普通 `/chat`、`/chat/stream`、`/runs` 和工单输入 API 都调用同一 Case 输入/执行链。即使旧 FAQ 配置为 inline，选定 Case 的执行仍然持久排队。checkpoint 重放和死信转人工也走同一提交检查。
- Case 流在成功提交前不发送模型正文增量，避免用户先看到最终被拒绝的旧草稿。进度和最终已提交结果仍支持 SSE。
- 已原子完成的同 attempt 清除租约后，心跳识别为正常收尾，仍发布终态并 ACK；真正过期/换 owner/换 attempt 的执行不能写业务结果。

旧结果被拒绝不等于业务解决；Run 成功结束也不等于 Case 已解决或模型答对。API 保留 `case_applied`、拒绝原因和业务状态来区分这些结果。

## 幂等与通知语义

同一 Case 的转人工动作以 `handoff:<case_id>` 持久去重，不随 Run 重试或新 Run 再建工单。通知 outbox 对同一 ticket 唯一；动作与 outbox 在结果事务中一起落库。业务输入/动作/结果回执不依赖短期 Run 留存，Run 清理后重复输入仍能取回原回执及已保存结果。

PostgreSQL 通知发送器增加数据库租约，多个新发送器不能同时领取同一到期通知；状态回写检查领取者。外部 webhook 是**可重试的至少一次投递语义，不是绝对仅发送一次**：接收方已收到但响应丢失、发送进程崩溃或租约过期时仍可能重发。使用固定通知 ID 作为 `Idempotency-Key`，接收端也必须去重。达到重试上限会保留 failed 记录，既有监控可见。测试证明的是数据库单条动作/outbox及正常并发领取去重，未证明外部接收端恰好处理一次。

本阶段的转人工去重范围是同一个 Case 的同一动作；重开本身不另发一份转人工通知。

指标新增 `helpdesk_case_rejections_total{reason}`、`helpdesk_case_actions_total{outcome}`，标签均由代码固定。沿用 Run 租约、等待、耗时和终态指标。

## 迁移与应用回滚

这是 PostgreSQL 的追加式迁移，沿用数据库启动迁移锁，以 `helpdesk_migrations` 记录 `case-v1`。保留旧 `tickets` 列形状、ID 和旧状态读取方式：

| 旧工单状态 | Case 状态 | 旧应用读取投影 |
| --- | --- | --- |
| open | open | open |
| assigned | in_progress | assigned |
| resolved | resolved | resolved |
| cancelled | cancelled | cancelled |
| — | waiting_for_info | open |

历史记录逐张回填，不按会话/订单合并。归属不明的历史记录保持 `instance=''`，普通员工 Case 的 get/list/modify 排除它们。管理员必须通过接纳接口，提供旧工单更新时间和归属依据，才能确认归属；不会因知道工单 ID 就自动认领到调用者实例。接纳保持工单 ID，并仅带入该工单自身的原始问题，不擅自关联整个会话。接纳后从人工控制开始。

未接纳的旧工单继续支持原状态流转，触发器同步 Case 投影。已启用 Case 管理的工单由数据库触发器阻止旧代码直接 UPDATE（SQLSTATE `55000`）；新版旧状态 API 返回 409。旧应用不能在缺少版本检查时覆盖新版 Case。

Case Run 在数据库 `agent_runs.instance` **和** Redis stream 中均使用 `<instance>:case-v1`。已测试旧 `PgRunStore` 的 due-for-dispatch 扫描、领取和派发标记路径拿不到这些 Run，防止旧 sweeper 把它们送回旧执行器。应用回滚时，新 Case 的执行必须等待支持该协议的版本恢复；不能交给旧 Worker 继续处理。

新 Case 的消息审计和详细执行事件也使用受保护的 Case 实例范围，旧共享密钥历史接口不会读到它们。新建工单在旧表仅保存通用提示，不保存 Case 原始消息。Case 执行事件直接写 PostgreSQL 的独立范围，不投递给只支持 staff/customer 的旧 Kafka topic/消费者；原 FAQ 事件链不变。

人工执行已有审计清理时，消息内容可以按原留存规则删除；Case 消息回执保留，seq 引用置空，不阻止其他审计清理。Case 事实、草稿和业务回执的专门留存政策需要阶段 5 与部署运维一起明确，当前没有额外的自动删除任务。

## 对后续阶段的接口

### 阶段 5：可信员工身份

`app.core.actor.ActorContext(actor_id, roles, instance)` 与 `get_current_actor(Request)` 是统一边界。只接受服务端认证中间件放入 `request.scope['actor']` 的 ActorContext；请求头、浏览器 user_id 和共享 key 都不能生成员工声明。员工/运营/管理员在同一已验证实例内协作，管理员单独有历史归属接纳权限；不宣称已实现人员行级隔离。

本阶段**没有实现员工登录**。未连接身份提供方时 Case API 返回 401，前端提示使用公司员工账号登录；自动测试显式覆盖该依赖。阶段 5 应在认证成功后注入 ActorContext，复用此依赖及实例约束，并完成统一角色规则。管理接口和浏览器下游 key 的整体治理仍属阶段 5。

运行新 Case 功能需要 PostgreSQL、Redis、明确的 staff/customer 实例和对应 Worker 角色。缺少条件时新接口不可用；**SQLite 新 Case 功能未实现、未记为通过**。原 SQLite 工单及 FAQ 路径仍保留并经过完整回归。

### 阶段 3：附件和核验证据

`CaseService.accept_input(actor, CaseInput, request_id=..., transport=...)` 是材料入口。首次输入使用 `new_case=true`；续输入使用 `case_id`、`expected_version`、`expected_control_version`；必须携带稳定的 `message_id` 和 `conv_id`。同一 ID 的重试应发送原体，不使用新版本重写旧请求。

`attachment_resolver(actor, ids)` 由附件服务注入，返回已重新授权的 `{id, sha256}`。未接入时带附件的输入返回 422，不假装上传完成。数据库只保存引用、哈希和来源消息。

可信核验服务可调用 `submit_evidence(actor, case_id, expected_version=..., expected_control_version=..., attachment_id=..., sha256=..., facts=..., evidence_id=...)`。该方法重新授权附件并核对哈希，幂等采纳已核实字段、递增版本、重算待补项、使旧草稿失效，重试返回首次采纳的版本。**没有向浏览器开放“自称已核实”的接口**。图片候选提取、业务核对和每次读取授权由阶段 3 实现；核验证据采纳后若需自动继续，应以新的稳定消息 ID 调用材料入口启动新 Run，不重用旧 Run 的材料快照。

### 公共 API

- `GET /tickets/cases`：当前已验证实例的 Case 列表，可按 conv_id 过滤。
- `POST /tickets/cases/inputs`：接受材料，返回 case_id/message_id/case_version/run_id/deduplicated。
- `GET /tickets/{id}/case`：完整 Case、消息、材料、草稿、事件和执行回执。
- `POST /tickets/{id}/case/actions`：带两个 expected 版本的 status/takeover/release/reopen。
- `POST /tickets/{id}/case/drafts/{draft_id}/approve`：带两个版本和 body_hash 的审批。
- `POST /tickets/{id}/case/adopt`：管理员显式接纳未知历史归属。
- `GET /tickets/cases/runs/{run_id}` 及 `/events`：可信员工读取执行结果/续流。
- 原 `/chat`、`/chat/stream`、`/runs` 支持相同 Case 字段；纯结构化材料/附件输入可省略 message。原 `/runs/{id}` 读取 Case Run 时也执行员工校验。

## 验证记录

所有后端和 SQL 测试均使用版本受控脚本的临时源码副本，环境从空开始，不读取实际 `.env`、模型设置或生产 DSN。PostgreSQL、Redis、Kafka 使用随机 loopback 端口的临时容器并自动删除。前端依赖安装、测试、构建也在临时副本，不改运行服务使用的 `frontend/dist`。

| 命令 / 项目 | 结果 |
| --- | --- |
| `scripts/ci-local.sh --full` | 当次后端 1,790 passed；SQL Agent、前端及构建全部通过 |
| 最终实现源码 `.claude/skills/test-backend/scripts/run.sh --full` | **1,791 passed，0 skipped**；包含最后的字段幂等、证据回执修正；ruff 与契约自检通过 |
| SQL Agent（上述 CI 调用 `run-sql-agent.sh --full`） | **1,239 unittest + 6 pytest passed，0 skipped**；另 41 个契约检查与前者重叠，不累加 |
| 前端（上述 CI 的临时副本） | **405 passed，38 个文件，0 skipped**；typecheck 与 production build 通过 |
| `git diff --check` | 通过 |

新增重点覆盖：两个独立 PostgreSQL 连接池的同消息并发去重和竞争补料；旧材料/接管/关闭/交回后的结果拒绝；租约失效、重新领取和等待锁期间过期；真实 Worker 的提交后心跳、checkpoint、死信及 ACK；跨 Run 单条动作/outbox和双通知发送器；证据授权失效与哈希/幂等；旧四状态迁移、未知归属接纳、旧写入阻止、旧扫描/领取隔离；四个公开入口在真实 PostgreSQL/Redis 下从缺料到续跑、审批、接管使审批失效；Case 快照/SQL 会话隔离；前端选择问题、补料冲突、网络重试、审批、接管、重开。

前端 6 个新增流程测试通过 DOM 与真实 API 客户端请求格式运行，响应夹具模拟服务；它们不是浏览器端到端测试。**本阶段未运行真实浏览器端到端、真实模型业务质量、200 场景评测、50 用户负载、镜像构建、部署或恢复演练**；不把 Mock、未执行或镜像构建跳过记作通过。浏览器及附件流程继续在阶段 3 验收，统一业务质量/性能门禁属阶段 6。

## 交接状态

阶段 2 实现与自动验证完成，等待主对话代码验收。实现范围内没有未提交业务文件；实施期间出现的本机 `.codex/config.toml` 不属于本阶段，未修改、未提交。登录、附件上传/图像核对、政策作用域、真实模型评测和生产上线均保持各自后续阶段的边界。
