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
**不由提问的客户决定**。打开 `SQL_AGENT_IMPERSONATE` 会把真实客户 id 传下去，
但下游的 impersonation 只换 `user_id`、保留服务密钥的角色与策略，
**不会为最终用户重新解析权限**——用两个客户身份实测过：同一把 key，可见数据相同。

**由此得出的三条部署纪律：**

- 下游那把 key 的策略要按**能接触到本客服系统的最不受信任的访客**来配。
  那把 key 能读到的东西，任何一个客户都有可能被告知。
- 不要把一个实例放在多个客户的数据前面并称之为隔离。真正的按客户隔离需要
  取服务权限与最终用户权限的**交集**，并且把租户/行范围下沉到数据库本身。
  这两件事本系统都没有做。
- `POST /auth/token` 会签发身份，它由共享的管理 key（`AI_HELPDESK_API_KEY`）把关；
  那把 key 证明的是"调用方是我们的基础设施"，不是"这个用户登录了"。
  生产模式下它还被绑定到来源白名单（`AI_HELPDESK_TOKEN_ISSUER_CIDRS`，校验的是
  直连对端而不是 `X-Forwarded-For`），只有网关能调到；`0.0.0.0/0` 这类通配会被拒。
  长期该由登录系统来签发这些令牌，对接需要什么写在 [auth-token-integration.md](auth-token-integration.md)。

生产模式（`APP_ENV=production`）下，`AI_HELPDESK_API_KEY`、`AI_HELPDESK_AUTH_SECRET`、
`AI_HELPDESK_PROTECT_CHAT` 缺任何一项进程都会拒绝启动——从前缺一项的含义是
"这个端点不需要鉴权"。同一套启动前置校验还要求 `AI_HELPDESK_TOKEN_ISSUER_CIDRS`
和上文的 `AI_HELPDESK_DEPLOYMENT_SCOPE`；五个必配项都由 `backend/app/core/preflight.py` 强制。
