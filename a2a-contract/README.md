<div align="center">

# A2A Contract

**A versioned, machine-checked contract between a support agent and a data agent.**

It defines how AI Helpdesk delegates a data question to AI SQL Agent over A2A, how each stage streams back over SSE, and when the result may back an answer: one protocol document, five JSON Schemas, 30 golden fixtures and a self-check, pinned by tag in both agents' CI.

[![Contract](https://img.shields.io/badge/Contract-v1.13-6E56CF?style=flat-square)](a2a-streaming-contract.md)
[![A2A](https://img.shields.io/badge/A2A_0.2-JSON--RPC_2.0_%2B_SSE-0D9488?style=flat-square)](docs/a2a-integration.md)
[![JSON Schema](https://img.shields.io/badge/JSON_Schema-2020--12-0A7CFF?style=flat-square)](schema/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](tools/check_contract.py)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/a2a-contract/actions/workflows/contract.yml)

**English** · [简体中文](README.zh-CN.md)

[Highlights](#highlights) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Features](#features) · [Integrate](#integrate)

</div>

<p align="center">
  <img src="docs/screenshots/delegation.webp" width="960" alt="An order-status question in the AI Helpdesk console is delegated to the SQL agent over A2A. On the left, the console lists the SQL agent's stages as they arrive (finding tables, planning, a query for each of two databases, a repair) and then answers, and Learning mode says the result passed verification and was used as evidence. On the right, the same turn's A2A frames arrive in order: the Agent Card, POST /a2a with message/stream, a task, a status update for each stage including two the console does not show, the artifact whose data part reads execution_status success, no risk and not degraded, and a final completed event, ending with the trust check's verdict that the result can be used as evidence" />
</p>

<p align="center"><sub>A made-up order on a mocked API. The frames on the right are replayed from the same mock and validate against this repository's schemas.</sub></p>

## Highlights

- **One payload, three transports.** `message/stream` (JSON-RPC 2.0 over SSE), `message/send` and plain `POST /query` carry the same data part, with 7 required fields and one set of trust rules, so a fallback or a rollback never changes what counts as evidence.
- **Delivered is not succeeded.** `completed` only says the payload arrived; `execution_status` (5 values) says whether the query ran, and risk, degradation or truncation can still rule a result out. A missing or mistyped field counts against it: `"false"` is not a boolean.
- **Checked by machine.** 5 JSON Schemas (Draft 2020-12) and 30 golden fixtures in 6 groups, 7 of them schema-valid but not plain evidence. `tools/check_contract.py` proves each negative fixture is rejected, each link and `§` reference resolves, and the version agrees in all 5 places; GitHub Actions runs it on every push to `main`, pull request and tag.
- **Timeouts set from measurements.** In 82 audited model calls, database queries averaged 17.7 s and the slowest took 68.3 s, so the client gives up after 20 s of silence or 90 s in total, and progress events are the heartbeat. A cancelled run stops at its next checkpoint: 0.07–2.7 s in 6 measured cancels.
- **Released like a public API.** Both agents pin `v1.13` in CI and fail rather than skip without it. A released tag's contract never changes: `v1.11` exists only to fix the version string `v1.10` shipped with.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="A2A Contract architecture: ai-helpdesk, the support agent, reads ai-sql-agent's Agent Card and calls POST /a2a with message/stream. While the SQL agent's loop calls the model and runs read-only SQL on four databases, it streams back over JSON-RPC 2.0 and SSE a task, a status update for each stage, one artifact with the data part and exactly one final event. The helpdesk's A2A client gives up after 20 s of silence or 90 s in total, judges the data part with assess() and streams progress and the answer to the console. The a2a-contract repository, version 1.13, defines every frame with the contract, five JSON Schemas, 30 golden fixtures and a self-check, and both repositories' CI pin its tag. Pulses travel each connection in that order" />
</p>

[AI Helpdesk](https://github.com/heyemlee/ai-helpdesk) runs the conversation, [AI SQL Agent](https://github.com/heyemlee/ai-sql-agent) queries business data, and this repository defines the interface between them; nothing here is deployed. For an order question, the helpdesk reads the Agent Card and opens `message/stream`. The SQL agent streams a status update per stage while its loop calls the model and runs read-only SQL, then one artifact and one final event, and the helpdesk judges the data part before a specialist may use it. Knowledge questions never leave the helpdesk. Blue marks requests and data, teal protocol events, orange model calls, violet the contract.

- **No keep-alive frames.** The client drops a stream after 20 s without an event, and every event the server sends is a real stage change, so a stalled query dies at 20 s instead of being pinged along to the 90 s cap.
- **Fall back only before the stream opens.** A `-32004` before the stream opens has cost nothing, so the client may resend with `message/send`. After that, a failure ends the call: resending would run the query twice and charge the quota twice.

## Tech stack

| Layer            | Technology                                         | Role                                                                                                |
| ---------------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Protocol         | A2A 0.2 subset · JSON-RPC 2.0 · Server-Sent Events | `message/stream` and `message/send` on `POST /a2a`; `task`, `status-update`, `artifact-update` events |
| Discovery        | Agent Card                                         | Served at `/.well-known/agent.json`; `capabilities.streaming` picks stream or send; cached 30 s      |
| Contract surface | JSON Schema Draft 2020-12                          | Data part, progress event, terminal event, stage names, the `*_db` identifier pattern               |
| Shared samples   | Golden fixtures · `fixtures/index.json`            | 30 payloads, each with its expected schema verdict, trust verdict and reason                        |
| Self-check       | Python 3.12 · jsonschema · referencing             | Schemas, fixtures, links and `§` references, version consistency                                    |
| CI               | GitHub Actions                                     | Runs the self-check here; both agents run the fixtures in their own CI against the pinned tag       |
| A2A client       | AI Helpdesk · FastAPI · httpx                      | Streams the call, enforces the 20 s and 90 s limits, one `assess()` for all three transports        |
| A2A server       | AI SQL Agent · FastAPI                             | Emits the stages, the data part and one terminal event; cancels a run when the caller disconnects   |

## Features

### Streaming protocol

- **Guaranteed order.** One `task`, any number of `status-update`s, one `artifact-update`, then exactly one terminal event: `completed`, `failed` or `canceled`.
- **Stages are the contract.** 9 fixed stage names, whatever the server calls them inside. `repair` comes at most once per step and steps only increase, so a repeated `repair` is a server bug the client can see.
- **Errors before the stream.** Bad keys get JSON `-32600`, rate limits and quotas 429 with `Retry-After` and `-32029`, a disabled stream `-32004`, all before any event. One `trace_id` comes back in the response header and every event.

| Measured                                                  | Result                              |
| --------------------------------------------------------- | ----------------------------------- |
| Database queries, 7 of 82 audited model calls             | 17.7 s on average                   |
| All 82 calls                                              | p50 6.6 s · p90 17.7 s · max 68.3 s |
| Longest gap between two server events, one end-to-end run | 9.5 s, under the 20 s silence limit |
| From cancel to stop, 6 runs on a real model               | 0.07–2.7 s                          |

Recorded in the contract (§2, §5.4 and its status notes). The whole exchange is also a [sequence diagram](docs/diagrams/a2a-sequence.en.svg), with an [interactive version](docs/diagrams/a2a-sequence.en.html).

### Result trust

- **Five outcomes.** `execution_status` is `success`, `partial_success`, `no_data`, `failed` or `not_applicable`: "found nothing" and "never ran" need different answers.
- **What may back an answer.** `completed`, a `success` or `partial_success` status, and `has_risk` and `degraded` both false. A partial result supports only its successful steps; a truncated one rules out totals and counts.
- **Missing means untrusted.** A missing `has_risk` or `degraded` counts as true and a missing `queried_database` as false; without `as_of`, no date is claimed.

### Golden fixtures and consumer tests

| Group | What it pins down                                                                       | Fixtures |
| ----- | --------------------------------------------------------------------------------------- | -------: |
| A     | Valid data parts, identical on all three transports                                     |        3 |
| B     | Schema rejects: missing trust fields, `"false"` strings, contradictions                 |        6 |
| C     | Schema-valid but not plain evidence: failed, partial, risky, replayed, truncated, empty |        6 |
| D     | Database identifiers: unknown but well-formed is accepted, malformed is rejected        |        2 |
| E     | Progress-event envelopes                                                                |        8 |
| F     | Terminal events; ordering is asserted on whole streams                                  |        5 |

- Each fixture is the raw payload; `fixtures/index.json` records its expected schema and trust verdicts and why it exists.
- Both agents read the same files at the pinned tag, never a copy, since copies drift.
- A and B must pass together: rejecting everything passes B and fails A. What each side asserts: [CONSUMER-TESTS.md](CONSUMER-TESTS.md) (in Chinese).

### Boundaries

- **A2A 0.2, a subset.** The Agent Card says `0.2`. A2A 1.0 dropped the `kind` field these events are built on, so moving up is a major contract change, not a new version string.
- **No answer deltas, ever.** Delegated text reaches users only through the helpdesk's own model and sentence guard; streaming it raw would bypass the guard.
- **One service identity.** Sessions stay separate, but what a query can see follows the SQL agent's service key, not the end user ([identity notes](https://github.com/heyemlee/ai-helpdesk/blob/main/docs/integrations/sql-agent-identity-requirements.md)).

## Integrate

```bash
git clone --depth 1 --branch v1.13 https://github.com/heyemlee/a2a-contract.git .contract  # never main
export A2A_CONTRACT_DIR="$PWD/.contract"  # where both agents' contract tests look first
export A2A_CONTRACT_REQUIRED=1            # fail instead of skipping when the contract is missing
```

To switch the agents to A2A, set `SQL_AGENT_TRANSPORT=a2a` (the default is `POST /query`) and `SQL_AGENT_TIMEOUT_S=90` in AI Helpdesk, plus `SQL_AGENT_API_KEY` if the SQL agent requires a key; streaming is on by default in AI SQL Agent. Budgets and fallbacks: [docs/a2a-integration.md](docs/a2a-integration.md) (in Chinese).

## Test

```bash
python3 -m venv .venv && .venv/bin/pip install jsonschema referencing  # the self-check's only dependencies
.venv/bin/python tools/check_contract.py           # schema, fixtures, links and version, as CI runs it
.venv/bin/python tools/check_contract.py fixtures  # one section: schema, fixtures, links or version
```

## Release

```bash
.venv/bin/python tools/check_contract.py version  # every place that states the version must agree
git tag vX.Y && git push origin vX.Y              # the last step: a released tag's contract never changes
```

A contract change is bilateral: both agents move their pins (2 places in AI SQL Agent, 3 in AI Helpdesk) and add the new cases. Procedure and history: [docs/maintenance.md](docs/maintenance.md#版本) (in Chinese).

| Tag     | Change                                                                                                                          |
| ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `v1.13` | A disconnect cancels the run at its next checkpoint; new `canceled` terminal state with a schema and 5 fixtures; 429 uses `-32029` |
| `v1.12` | `execution_status` becomes required; `dp-trust-01`, which lacks it, moves to group B                                            |
| `v1.11` | Same contract as `v1.10` with its version string corrected; pin this instead of `v1.10`                                         |
