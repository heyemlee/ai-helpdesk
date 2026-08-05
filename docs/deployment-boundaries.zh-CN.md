# 部署与安全边界

[← README](../README.zh-CN.md) · [English](deployment-boundaries.md)

## 安全边界：本系统是单租户

**本系统按单租户部署，不要对外表述为多租户隔离。** 之所以要把边界单独写下来：
"提问的是谁"和"这个人能看到什么"是在两个地方回答的，而只有前一个真正落实了。

**这条边界现在由启动断言强制，不再只是文档。** 生产模式下必须显式声明
`AI_HELPDESK_DEPLOYMENT_SCOPE`（`internal_single_tenant` | `multi_tenant`），
否则进程起不来——**故意不给默认值**，因为"漏配了"和"想清楚了配成这个"风险完全不同。
声明单租户却同时打开 `SQL_AGENT_IMPERSONATE` 也会被拒：那个组合说明有人**以为**
这套部署在隔离业务数据，而那个误解本身就是风险。
实现见 [`check_deployment_scope`](../backend/app/core/preflight.py)。

**隔离到位的部分。** 对话记忆、情景记忆、用户画像和限流都按 `user_id` 分隔，
这个 id 由服务端从 HMAC 签名令牌推导（`AI_HELPDESK_AUTH_SECRET`），客户端伪造不了。
发给下游数据服务的会话标识也由这个可信身份派生，所以一个客户无法指定另一个客户的
下游会话。

**没有隔离的部分：业务数据。** 订单、生产、库存、账款的答案来自另一个服务
（`ai-sql-agent`），本系统是委派过去查的。默认情况下**所有查询都以同一个服务身份发出**
（`SQL_AGENT_USER_ID`），因此能查到什么由**那把 key 的角色策略**决定，
**不由提问的人决定**。

"按客户隔离业务数据"其实是**三件互不代表的事**。下面是对照 `ai-sql-agent`
当时的代码（只读核对，没有改动、也没有重新联调）得到的现状：

| | 是什么 | 现状 |
| --- | --- | --- |
| **1. 服务权限 ∩ 最终用户权限** | 代查（impersonation）时，下游按**最终用户**再解析一次策略，与服务 key 的策略取交集；解析不出这个人就拒绝，不回落到服务身份 | **下游代码里已经有，本系统这一侧没有启用，也没有复测。** `ai-sql-agent` 的 `ApiKeyStore._apply_user_policy` 与 `IntersectionPolicy` 实现了表、列两级的交集，`users` 段里没声明的用户得到全拒绝。它要三个条件**同时**成立才生效：下游那把 key 标了 `can_impersonate`、下游的 key 配置里有 `users` 段、本系统打开了 `SQL_AGENT_IMPERSONATE`。缺 `users` 段时下游仍是旧行为——换 `user_id` 不换权限（下游只在生产模式、`multi_tenant` 范围的启动检查里拒绝这种配置）。 |
| **2. 客户身份接入** | 一个可信的"这是哪个客户"，加上"这个客户在下游对应哪个角色、哪个租户"的映射 | **没有。** 本系统的令牌里只有 `user_id`，由 `/auth/token` 凭共享管理 key 签发，没有接登录系统；没有客户号或租户 claim。下游的 `users` 段是 key 配置文件里一张静态的"用户 → 角色"表，没有人维护客户名单。面向客户的那个实例根本不接 `ai-sql-agent`。 |
| **3. 行级隔离** | 同一张表里，甲客户只看得到甲的行，由数据库自己执行 | **没有启用。** 下游的策略模型有行范围（`row_filters`），PostgreSQL 执行器会把最终用户 id 放进事务（`app.user_id`），`ops/least_privilege.py` 能从策略生成 RLS 语句——但那些语句要在真实业务库上执行过才算数，只对 PostgreSQL 有效（SQLite 执行器没有任何行级限制），下游的 SQL 授权关卡只管表和列、不会改写查询去加行条件，而且 `internal_single_tenant` 范围下这一项不做要求。 |

还有一条**可观测性**上的缺口，它决定了上面三项"配上了"之后我们信不信得过：下游的响应里
没有"这一次实际用的是哪个身份、哪份策略"的字段（我们在
[sql-agent-identity-requirements.md](integrations/sql-agent-identity-requirements.md)
第 3 条提的 `principal_source` 等）。请求了代查、对面却退回成服务身份——本系统今天
没有任何字段能发现。

所以要分清：

- **第 1 项在下游有了实现，不等于这条链路可以称作按客户隔离。** 那还需要第 2、3 项、
  上面的可观测字段，以及一次用两个不同客户身份做的端到端复测。
- 早先那次实测（两个客户身份、同一把 key、可见数据相同）是在下游加上交集**之前**做的。
  它不再能说明下游现在的行为；**但之后没有人重测过**，所以本文档不写"现在已经按用户区分了"。
- 在 `internal_single_tenant` 范围下——本系统第一次上线定的就是这个范围——三项**都不启用**：
  生产模式下本系统拒绝同时打开 `SQL_AGENT_IMPERSONATE`，下游在生产模式的同一范围下也拒绝带
  `can_impersonate` 的 key。所有人看到的业务数据范围相同。

**由此得出的三条部署纪律：**

- 下游那把 key 的策略要按**能接触到本客服系统的最不受信任的访客**来配。
  那把 key 能读到的东西，任何一个提问的人都有可能被告知。
- 不要把一个实例放在多个客户的数据前面并称之为隔离。真正的按客户隔离要上表三项
  **同时**成立并且复测过；今天第 1 项只存在于下游的代码里，第 2 项不存在，第 3 项没有启用。
- `POST /auth/token` 会签发身份，它由共享的管理 key（`AI_HELPDESK_API_KEY`）把关；
  那把 key 证明的是"调用方是我们的基础设施"，不是"这个用户登录了"。
  生产模式下它还被绑定到来源白名单（`AI_HELPDESK_TOKEN_ISSUER_CIDRS`，校验的是
  直连对端而不是 `X-Forwarded-For`），只有网关能调到；`0.0.0.0/0` 这类通配会被拒。
  长期该由登录系统来签发这些令牌，对接需要什么写在 [auth-token-integration.md](auth-token-integration.md)。

生产模式（`APP_ENV=production`）下，`AI_HELPDESK_API_KEY`、`AI_HELPDESK_AUTH_SECRET`、
`AI_HELPDESK_PROTECT_CHAT` 缺任何一项进程都会拒绝启动——从前缺一项的含义是
"这个端点不需要鉴权"。同一套启动前置校验还要求 `AI_HELPDESK_TOKEN_ISSUER_CIDRS`
和上文的 `AI_HELPDESK_DEPLOYMENT_SCOPE`；五个必配项都由 `backend/app/core/preflight.py` 强制。

## 员工工作台与客户入口

前端（`frontend/`）是**员工工作台**：对话、知识库、工单，加上「管理与诊断」。它不是客户入口，
也不能靠一个界面开关变成客户入口——谁能看什么是后端判的。

**员工侧已经由后端把住的：** 知识文档的列表、正文与分类（`GET /knowledge/documents*`、
`GET /knowledge/categories`、`PATCH /knowledge/documents/{doc_id}`）、按问题检索
（`POST /search`）、知识导入、SOP 正文（`GET /sops`）、工单与对话原文，全部要求 `X-API-Key`。
配了 `AI_HELPDESK_API_KEY` 的实例（面向客户的那个就是）上，不带钥匙一律 401；对外的 nginx 只放行
`/chat`、`/chat/stream`、`/health`。所以内部的采购、成本规则和 SOP 没有任何一条匿名可读的接口。

**客户入口还缺的，按接入顺序：**

1. **客户身份。** 没配 `AI_HELPDESK_AUTH_SECRET` 时 `user_id` 取自请求体，任何人可以自称任何人。
   需要登录系统或网关来签发 `X-User-Token`，见 [auth-token-integration.md](auth-token-integration.md)。
   在它出现之前，下面几条都没有可以挂靠的"这个客户是谁"。
2. **知识的受众。** 两个实例读的是同一张 `kb_chunks` / `kb_documents`，文档没有"内部 / 可对客"之分，
   客户实例的检索会把内部资料当作回答依据。接入点：给 `kb_documents` 加受众字段（默认内部），
   `kb_search` / `kb_search_lexical` 按实例配置过滤；新导入的文档默认不对客，要有人明确放行。
3. **SOP。** 两个实例挂的是同一个 `backend/skills`，客户实例的 Agent 系统提示里带着内部的审批边界和
   话术。接入点：客户实例用单独的 `AI_HELPDESK_SKILLS_DIR`。
4. **业务数据。** 客户实例不接 `ai-sql-agent`（`SQL_AGENT_URL` 为空），订单问题只按通用规则回答。
   要让客户查自己的订单，上文表里的三项都要到位：先有第 1 条的身份并把它映射到下游的用户与角色
   （客户身份接入），再让下游按这个人取权限交集，最后把"只看自己的订单"落到数据库的行范围上。
   下游今天只有中间那一项的代码；只完成其中任何一项都不构成按客户隔离。
5. **客户自己的工单和历史。** `/tickets`、`/conversations/{conv_id}` 是员工接口。客户要看到
   "我的工单到哪了"，需要按已验证身份过滤的只读接口；`conv_id` 不能当凭证用。
6. **客户页面。** 仓库里没有客户用的聊天页面（对外入口的 `/` 是 404）。它应该是单独的一套页面，
   只调 `/chat` 与 `/chat/stream`，不带员工工作台的代码和钥匙。
7. **转人工之后。** 没配 `TICKET_WEBHOOK_URL` 时转人工只落工单、不通知任何人。固定文案因此只说
   "需要人工处理"，不承诺有人联系；要对客户承诺跟进，先把通知渠道和值班流程接上。
8. **对外的 `/health`。** 它返回 Agent、阈值和"接了什么"的内部信息，对外入口上应换成只报存活的版本。

以上任何一条没接上之前，客户入口不应对外表述为"可以上线"。
