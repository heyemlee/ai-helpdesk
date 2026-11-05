---
name: test-backend
description: Run the backend gate (ruff + pytest + the A2A contract self-check) and the SQL Agent tests in throwaway copies, so the real backend/.env, ai-sql-agent/.env, API keys and production databases can't reach the tests. Use this whenever you change code under backend/, ai-sql-agent/, a2a-contract/, docker-compose.yml, deploy/ or scripts/ and need to know whether tests pass, before opening a PR, when a Backend, A2A contract or SQL Agent job fails in CI, and whenever the user says 跑后端测试, 跑一下测试, 过一遍 CI or pytest. Use it instead of running pytest inside backend/ or ai-sql-agent/.
---

# Test the backend

Don't run pytest inside `backend/`. `load_dotenv()` in `app/api/main.py` finds the real `backend/.env`, so the tests would pick up real keys, the model relay and the production database DSN. The same goes for `ai-sql-agent/`: under unittest its `conftest.py` doesn't load, so `ai-sql-agent/.env` and the saved model settings in `runtime_data/` would reach the tests. Both scripts avoid that in two ways:

- They copy only the files git knows about into a temp directory, so anything gitignored (`.env`, the plaintext model settings in `backend/data/` and `ai-sql-agent/runtime_data/`, logs) can't get in. Uncommitted edits to tracked files and new untracked files are included, so they test what you're about to commit.
- They start the tests from an empty environment, so variables like `ANTHROPIC_BASE_URL` or `OPENAI_API_KEY` in your shell don't reach them.

The A2A contract lives in the repository at `a2a-contract/`, so the copy carries the contract from the same commit as the code. Both scripts point the tests at it and set `A2A_CONTRACT_REQUIRED=1`: a contract test that can't find the contract fails instead of skipping.

## Run it

From the repository root, or the root of a worktree:

| Command | What runs | Where |
|---|---|---|
| `.claude/skills/test-backend/scripts/run.sh` | The contract self-check (`a2a-contract/tools/check_contract.py`), ruff, and all backend tests including the contract fixtures. Tests that need Postgres, Redis or Kafka skip. | In the sandbox |
| `.claude/skills/test-backend/scripts/run.sh tests/test_foo.py -k bar` | The self-check and ruff, then only what you name. Arguments go straight to pytest. | In the sandbox |
| `.claude/skills/test-backend/scripts/run.sh --full` | Everything above, plus disposable pgvector, Redis and Kafka containers, like CI's Backend and A2A contract jobs. Containers use random loopback ports and are removed afterwards. | Outside the sandbox: it needs the Docker socket and loopback ports |
| `.claude/skills/test-backend/scripts/run-sql-agent.sh` | The SQL Agent tests from CI's SQL Agent job: unittest (contract cases included), `tests/test_simple_erp.py`, and a check that no contract case skipped. pgvector tests skip. | Outside the sandbox: some tests start mock model servers on loopback ports or run node |
| `.claude/skills/test-backend/scripts/run-sql-agent.sh --full` | The same, plus a disposable pgvector container. | Outside the sandbox |

`run-sql-agent.sh` needs a Python 3.11 venv with `ai-sql-agent[dev]` installed. It uses `SQL_AGENT_PY` if set, otherwise the main checkout's `ai-sql-agent/.venv` (the one `scripts/dev-all.sh` uses). The copy's code shadows the installed package, and the script fails if it doesn't.

Use the offline mode while iterating. Use `--full` when a change touches a store, queued runs or the event log, because the tests that cover those are the ones that skip offline, and for a final check before opening a PR on backend changes.

## Read the result

- Exit code 0 means ruff and pytest both passed. A failure exits non-zero, and the temp copy and containers are removed either way.
- `-rs` prints every skipped test and why. Skips for missing services are expected offline. In `--full` a skip means something wasn't exercised, so look at it and report it. Don't call that run complete.
- When you report results, say which mode ran and give the counts, for example "offline: 1557 passed, 32 skipped". "Tests pass" alone hides what was skipped.

## Limits

- The Frontend and Docker images jobs aren't covered, and neither are the SQL Agent job's demos, benchmark smoke, release-gate smoke and read-only account checks. CI runs them on the PR.
- The tests get a placeholder key, or none, and make no real model calls. If a test seems to need real credentials, the test is wrong. Don't pass them in.
- Never point these tests at production's Postgres (5433) or Redis (6379). They create and drop their own data.
- `scripts/ci-local.sh` calls both scripts for its backend, contract and SQL Agent steps (`--full` passes through), then runs the Frontend job, and with `--with-images` builds the three images, in throwaway copies of its own. Use it when you want every job checked locally before pushing.
