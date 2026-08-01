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
**decided by the key, not by the person asking**.

"Isolating business data per customer" is **three separate things, and none of them
implies the others**. This is where each one stood when last checked, read-only
against the `ai-sql-agent` code of that day (nothing there was changed, and the two
services were not re-tested together):

| | What it is | Where it stands |
| --- | --- | --- |
| **1. Service permission ∩ end-user permission** | When the helpdesk asks on someone's behalf (impersonation), the downstream resolves a policy for **that end user** and intersects it with the service key's policy; an end user it cannot resolve is refused, not served as the service identity | **Present in the downstream code; not enabled on this side, and not re-tested.** `ApiKeyStore._apply_user_policy` and `IntersectionPolicy` in `ai-sql-agent` implement the intersection at table and column level, and an end user missing from the `users` section gets deny-all. It only takes effect when three things hold **at once**: the downstream key has `can_impersonate`, the downstream key file has a `users` section, and this system has `SQL_AGENT_IMPERSONATE` on. Without a `users` section the downstream still behaves the old way: the `user_id` changes, the permissions do not (the downstream refuses that configuration only in production mode under `multi_tenant`). |
| **2. Customer identity integration** | A trustworthy "which customer is this", plus the mapping from that customer to a downstream role and tenant | **Not there.** This system's token carries only a `user_id`, minted by `/auth/token` on the shared admin key; no login system is connected, and there is no customer-number or tenant claim. The downstream `users` section is a static user → roles table in the key file; nobody maintains a customer list in it. The customer-facing instance is not connected to `ai-sql-agent` at all. |
| **3. Row-level isolation** | Within one table, customer A sees only A's rows, enforced by the database itself | **Not enabled.** The downstream policy model has a row scope (`row_filters`), its PostgreSQL executor puts the end-user id into the transaction (`app.user_id`), and `ops/least_privilege.py` can generate RLS statements from a policy. But those statements only count once they have been run on the real business database, they exist for PostgreSQL only (the SQLite executor has no row-level restriction of any kind), the downstream SQL authorization gate checks tables and columns and never rewrites a query to add a row condition, and under `internal_single_tenant` the row scope is not required. |

There is also an **observability** gap, and it decides whether the three items can be
trusted once they are configured: the downstream response has no field saying which
identity and which policy were actually used for this request (`principal_source` and
its siblings, item 3 of
[sql-agent-identity-requirements.md](integrations/sql-agent-identity-requirements.md)).
If this system asks for impersonation and the other side quietly falls back to the
service identity, nothing in the response reveals it today.

So keep these apart:

- **Item 1 existing downstream does not make this path per-customer isolation.** That
  still needs items 2 and 3, the observability fields above, and one end-to-end re-test
  with two different customer identities.
- The earlier measurement (two customer identities, one key, same visible data) was
  taken **before** the downstream added the intersection. It no longer describes what
  the downstream does now; **but nobody has re-measured since**, so this document does
  not claim that data is now separated per user.
- Under `internal_single_tenant` — the scope chosen for this system's first rollout —
  **none of the three is switched on**: in production this system refuses to start with
  `SQL_AGENT_IMPERSONATE` on, and the downstream, in production under the same scope,
  refuses a key that has `can_impersonate`. Everyone sees the same range of business data.

**What follows from that.**

- Scope the downstream key's policy to *the least-trusted visitor who can reach this
  helpdesk*. Anything that key can read, anyone who asks can potentially be told.
- Do not deploy one instance in front of several customers' data and call it
  isolation. Real per-customer separation needs all three items in the table **at the
  same time**, and a re-test. Today item 1 exists only in the downstream code, item 2
  does not exist, and item 3 is not enabled.
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

## Staff workspace vs. customer entrance

The frontend (`frontend/`) is the **staff workspace**: conversations, knowledge, tickets, plus
"Admin & diagnostics". It is not a customer entrance, and a UI switch cannot turn it into one —
who may see what is decided by the backend.

**What the backend already enforces for staff data.** Document list and text
(`GET /knowledge/documents*`), question-based retrieval (`POST /search`), knowledge import, SOP
text (`GET /sops`), tickets and conversation transcripts all require `X-API-Key`. On an instance
with `AI_HELPDESK_API_KEY` set (the customer-facing one is), requests without the key get 401, and
the public nginx only forwards `/chat`, `/chat/stream` and `/health`. Internal purchasing and cost
rules and SOPs are therefore not readable through any anonymous endpoint.

**What the customer entrance still lacks, in the order it has to be built:**

1. **Customer identity.** Without `AI_HELPDESK_AUTH_SECRET`, `user_id` comes from the request body
   and anyone can claim to be anyone. A login system or gateway has to issue `X-User-Token`; see
   [auth-token-integration.md](auth-token-integration.md). Everything below needs it.
2. **Knowledge audience.** Both instances read the same `kb_chunks` / `kb_documents`, and documents
   carry no internal/customer-safe distinction, so retrieval on the customer instance grounds
   answers in internal material. Integration point: an audience field on `kb_documents` (default
   internal), filtered in `kb_search` / `kb_search_lexical` per instance; newly imported documents
   stay internal until someone explicitly releases them.
3. **SOPs.** Both instances mount the same `backend/skills`, so customer-instance agents carry
   internal approval limits and scripts in their system prompt. Integration point: a separate
   `AI_HELPDESK_SKILLS_DIR` for the customer instance.
4. **Business data.** The customer instance is not connected to `ai-sql-agent` (`SQL_AGENT_URL` is
   empty), so order questions get general rules only. Letting customers look up their own orders
   needs all three items in the table above: the identity from item 1 here, mapped to a downstream
   user and role (customer identity integration); the downstream intersecting permissions for that
   person; and "only my own orders" pushed down into the database's row scope. Today the downstream
   has code for the middle one only. Completing any single one of them is not per-customer isolation.
5. **A customer's own tickets and history.** `/tickets` and `/conversations/{conv_id}` are staff
   endpoints. "Where is my ticket" needs read-only endpoints filtered by verified identity;
   `conv_id` must not serve as a credential.
6. **A customer page.** The repository has no customer chat page (the public `/` is 404). It should
   be a separate bundle that calls only `/chat` and `/chat/stream`, without staff workspace code
   or keys.
7. **After a handoff.** Without `TICKET_WEBHOOK_URL`, a handoff only stores a ticket and notifies
   nobody. The fixed handoff notice therefore says only that a person needs to handle the request
   and promises no contact; wire up the notification channel and an on-call process before
   promising customers any follow-up.
8. **Public `/health`.** It returns agents, thresholds and capability flags; the public entrance
   should serve a liveness-only variant.

Until these are in place, the customer entrance should not be described as ready to go live.
