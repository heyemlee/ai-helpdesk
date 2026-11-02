---
name: test-backend
description: Run the backend gate (ruff + pytest) in a throwaway copy, so the real backend/.env, API keys and production databases can't reach the tests. Use this whenever you change code under backend/, docker-compose.yml, deploy/ or scripts/ and need to know whether tests pass, before opening a PR, when a Backend or contract job fails in CI, and whenever the user says 跑后端测试, 跑一下测试, 过一遍 CI or pytest. Use it instead of running pytest inside backend/.
---

# Test the backend

Don't run pytest inside `backend/`. `load_dotenv()` in `app/api/main.py` finds the real `backend/.env`, so the tests would pick up real keys, the model relay and the production database DSN. `run.sh` avoids that in two ways:

- It copies only the files git knows about into a temp directory, so anything gitignored (`.env`, the plaintext model settings in `backend/data/`, logs) can't get in. Uncommitted edits to tracked files and new untracked files are included, so it tests what you're about to commit.
- It starts the tests from an empty environment, so variables like `ANTHROPIC_BASE_URL` in your shell don't reach them.

## Run it

From the repository root, or the root of a worktree:

| Command | What runs | Where |
|---|---|---|
| `.claude/skills/test-backend/scripts/run.sh` | ruff, all tests, and the A2A contract fixtures at the pinned tag. Tests that need Postgres, Redis or Kafka skip. | In the sandbox |
| `.claude/skills/test-backend/scripts/run.sh tests/test_foo.py -k bar` | ruff, then only what you name. Arguments go straight to pytest. | In the sandbox |
| `.claude/skills/test-backend/scripts/run.sh --full` | Everything above, plus disposable pgvector, Redis and Kafka containers, like CI's Backend and contract jobs. Containers use random loopback ports and are removed afterwards. | Outside the sandbox: it needs the Docker socket and loopback ports |

Use the offline mode while iterating. Use `--full` when a change touches a store, queued runs or the event log, because the tests that cover those are the ones that skip offline, and for a final check before opening a PR on backend changes.

## Read the result

- Exit code 0 means ruff and pytest both passed. A failure exits non-zero, and the temp copy and containers are removed either way.
- `-rs` prints every skipped test and why. Skips for missing services are expected offline. In `--full` a skip means something wasn't exercised, so look at it and report it. Don't call that run complete.
- When you report results, say which mode ran and give the counts, for example "offline: 1557 passed, 32 skipped". "Tests pass" alone hides what was skipped.

## Limits

- The Frontend and Docker images jobs aren't covered. CI runs them on the PR.
- The tests get a placeholder key and make no real model calls. If a test seems to need real credentials, the test is wrong. Don't pass them in.
- Never point these tests at production's Postgres (5433) or Redis (6379). They create and drop their own data.
- `scripts/ci-local.sh` runs in place, so it reads the real `.env`, and its `npm run build` rewrites `frontend/dist`. Use this skill for the backend instead.
