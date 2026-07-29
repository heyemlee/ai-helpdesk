# Architecture diagrams

[← English README](../../README.md) · [← 中文 README](../../README.zh-CN.md)

The README overview is generated with [Archify](https://github.com/tt-a1i/archify), pinned to commit `5289f6867f048a7450ec5718f58459613a84cf41`. Its bundled viewer is distributed under the [MIT license](ARCHIFY-LICENSE.txt).

| Language | Editable source | Interactive viewer | GitHub image |
| --- | --- | --- | --- |
| 中文 | [JSON](architecture.archify.json) | [HTML](architecture.html) | [SVG](architecture.svg) |
| English | [JSON](architecture.en.archify.json) | [HTML](architecture.en.html) | [SVG](architecture.en.svg) |

Download an HTML file and open it locally for zoom, node inspection, themes and export. GitHub repository file pages do not execute the viewer. The SVG exports respond to the host's light/dark color preference.

## Scope & source map

This is a logical runtime overview, not a separate microservice for every box. The API prepares memory, intent, retrieval and optional business queries before invoking the agent orchestrator. The RAG box represents that preparation dependency; answer guards execute within agents, followed by confidence-based handoff. Arrows show selected dependencies and processing stages, not every database read/write or response packet.

| Diagram element | Source |
| --- | --- |
| Console / POST SSE | [App.tsx](../../frontend/src/App.tsx), [useChatStream.ts](../../frontend/src/hooks/useChatStream.ts) |
| API / identity / request preparation | [main.py](../../backend/app/api/main.py), [auth.py](../../backend/app/core/auth.py) |
| Intent / specialist orchestration | [intent_recognizer.py](../../backend/app/core/intent_recognizer.py), [agent_orchestrator.py](../../backend/app/agents/agent_orchestrator.py) |
| RAG / PostgreSQL | [knowledge_base.py](../../backend/app/tools/knowledge_base.py), [tool_manager.py](../../backend/app/tools/tool_manager.py), [pg.py](../../backend/app/storage/pg.py) |
| Redis / memory / shared state | [conversation_memory.py](../../backend/app/memory/conversation_memory.py), [shared_state.py](../../backend/app/core/shared_state.py) |
| Answer policy / tickets | [answer_policy.py](../../backend/app/core/answer_policy.py), [ticket_store.py](../../backend/app/tickets/ticket_store.py), [notifier.py](../../backend/app/tickets/notifier.py) |
| Models / telemetry / integrations | [llm_utils.py](../../backend/app/core/llm_utils.py), [telemetry.py](../../backend/app/core/telemetry.py), [sql_agent_client.py](../../backend/app/tools/sql_agent_client.py), [Compose](../../docker-compose.yml) |

The view deliberately keeps model providers and telemetry grouped. PostgreSQL also persists memory, profiles, tickets and audit, beyond the pictured RAG dependency. Optional `ai-sql-agent` delegation and evaluation are described in the cards and READMEs. Detailed request and deployment views remain in [architecture.md](../architecture.md).

## Regenerate

Obtain the pinned generator outside this repository:

```bash
git clone https://github.com/tt-a1i/archify.git /tmp/archify
git -C /tmp/archify checkout 5289f6867f048a7450ec5718f58459613a84cf41
```

Edit the corresponding JSON, then run from this repository's root:

```bash
node /tmp/archify/archify/bin/archify.mjs validate architecture docs/diagrams/architecture.archify.json --quality showcase --json
node /tmp/archify/archify/bin/archify.mjs deliver architecture docs/diagrams/architecture.archify.json docs/diagrams/architecture.html --quality showcase --json
node /tmp/archify/archify/bin/archify.mjs visual-check docs/diagrams/architecture.html --json
```

Repeat with `architecture.en` for English. Open the delivered HTML, choose **Export → SVG**, and replace the matching SVG file. Do not hand-edit generated HTML or SVG. Recheck both themes and refresh the hashes and results in [validation.json](validation.json) after regeneration. Browser-check screenshots are local review artifacts, not needed by the README.

The checked-in receipt distinguishes deterministic validation (9/9 showcase checks, zero errors/warnings), automated browser containment/readability (four desktop viewports), and visual review. It describes these diagram artifacts only, not application test coverage.

## Replace the screenshot

Save a real screenshot as `docs/images/main-interface.png`, then change `docs/images/screenshot-placeholder.svg` to that path in both root READMEs. The placeholder is an intentional empty slot, not a mock screenshot. A wide capture showing chat and the routing inspector works well; remove the placeholder caption when adding the real image.
