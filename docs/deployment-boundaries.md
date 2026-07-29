# Deployment boundaries

[← README](../README.md) · [简体中文](deployment-boundaries.zh-CN.md)

## Security boundary: single tenant by design

**This system is a single-tenant deployment. Do not describe it as multi-tenant
isolation.** The boundary is written down here because the two halves of "who is
asking" and "what they are allowed to see" are answered in different places, and
only the first one is enforced.

**This boundary is now enforced at startup, not just documented.**
`AI_HELPDESK_DEPLOYMENT_SCOPE` must be declared explicitly in production
(`internal_single_tenant` | `multi_tenant`) or the process refuses to start —
there is deliberately no default, because "forgot to set it" and "decided on
this value" carry different risk. Declaring `internal_single_tenant` while
`SQL_AGENT_IMPERSONATE=true` is also refused: that combination means somebody
*believes* this deployment isolates business data, and that belief is the risk.
See [`check_deployment_scope`](../backend/app/core/preflight.py).

**What is isolated.** Conversation memory, episodic memory, user profiles and rate
limits are keyed by a `user_id` that the server derives from an HMAC-signed token
(`AI_HELPDESK_AUTH_SECRET`). A client cannot forge it. The session key sent to the
downstream data service is derived from that same trusted identity, so one customer
cannot address another customer's downstream conversation.

**What is not isolated: business data.** Order, production, stock and payment
answers come from a separate service (`ai-sql-agent`) that this system delegates to.
By default every query is sent under **one shared service identity**
(`SQL_AGENT_USER_ID`), so what comes back is whatever that key's role policy allows —
**decided by the key, not by the customer asking**. Turning on `SQL_AGENT_IMPERSONATE`
forwards the real customer id, but downstream impersonation only swaps the `user_id`;
it keeps the service key's roles and policies and does not re-resolve permissions for
the end user. Verified with two customer identities: same key, same visible data.

**What follows from that.**

- Scope the downstream key's policy to *the least-trusted visitor who can reach this
  helpdesk*. Anything that key can read, any customer can potentially be told.
- Do not deploy one instance in front of several customers' data and call it
  isolation. Real per-customer separation needs the intersection of the service
  permission and the end-user permission, with the tenant/row scope pushed down into
  the database itself. That is not implemented here.
- `POST /auth/token` mints identities and is gated by the shared admin key
  (`AI_HELPDESK_API_KEY`), which proves "the caller is our infrastructure", not "this
  user logged in". In production it is additionally bound to a source allowlist
  (`AI_HELPDESK_TOKEN_ISSUER_CIDRS`, checked against the direct peer, not
  `X-Forwarded-For`), so only your gateway can reach it; wildcards like `0.0.0.0/0`
  are rejected. The long-term fix is for a real login system to issue these tokens —
  what that integration needs is written down in [auth-token-integration.md](auth-token-integration.md).

In production mode (`APP_ENV=production`) the process refuses to start unless
`AI_HELPDESK_API_KEY`, `AI_HELPDESK_AUTH_SECRET` and `AI_HELPDESK_PROTECT_CHAT` are
all set — a missing one used to mean "this endpoint needs no auth". The same
preflight also requires `AI_HELPDESK_TOKEN_ISSUER_CIDRS` and
`AI_HELPDESK_DEPLOYMENT_SCOPE` (see above); all five required variables are enforced in
`backend/app/core/preflight.py`.
