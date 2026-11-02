---
name: deploy
description: Deploy ai-helpdesk to production, the Docker Compose stack on this machine. Rebuilds only the named services from a clean export of a commit on main, keeps a rollback tag, and verifies that port 80 still reaches the customer instance. Only runs when the user invokes it.
disable-model-invocation: true
argument-hint: "<staff|customer|frontend ...> [commit, default origin/main]"
---

# Deploy to production

Requested: $ARGUMENTS

Typing this command is the go-ahead for the services it names, at the commit it names or otherwise the tip of `origin/main`. If it names no services, or anything is unclear, ask before you change anything.

Production shares its checkout with development, so what's checked out affects what runs: Compose reads `docker-compose.yml` and the gitignored env files from it, and containers bind-mount `deploy/` from it. Images, though, always come from a clean export, because the checkout can hold other sessions' uncommitted work.

Docker and the loopback ports aren't reachable from the sandbox, so run these commands outside it. Builds take minutes, so run them in the background. Shell variables don't carry over between commands, so set `ROOT`, `SCRIPTS`, `SHA` and `EXPORT` again as needed.

| Say | Compose service | Container | Image | Port |
|---|---|---|---|---|
| staff | `ai-helpdesk` | `ai-helpdesk-app` | `ai-helpdesk-ai-helpdesk` | 127.0.0.1:8080 |
| customer | `ai-helpdesk-customer` | `ai-helpdesk-customer` | `ai-helpdesk-ai-helpdesk-customer` | 127.0.0.1:8081, and :80 through nginx |
| frontend | `frontend` | `ai-helpdesk-frontend` | `ai-helpdesk-frontend` | 127.0.0.1:5174 |

## 1. Check before touching anything

```bash
ROOT="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"   # the main checkout
SCRIPTS="$ROOT/.claude/skills/deploy/scripts"
git -C "$ROOT" fetch origin
git -C "$ROOT" status -sb
SHA="$(git -C "$ROOT" rev-parse --short origin/main)"   # or the commit the user named
git -C "$ROOT" merge-base --is-ancestor "$SHA" origin/main
gh api "repos/heyemlee/ai-helpdesk/commits/$SHA/check-runs" --jq '.check_runs[] | [.name, .conclusion] | @tsv'
```

Stop and ask if any of these is off:

- **The commit isn't on `origin/main`, or its CI isn't all `success`.** Don't deploy something the required checks haven't passed.
- **The checkout isn't on `main`, is dirty, or is behind.** Don't switch branches or pull on your own: on 2026-10-21 nginx restarted while the checkout was on a feature branch and loaded that branch's config. Once you have the go-ahead to pull, note that if the pull changes `deploy/nginx/nginx.conf`, the file's single-file bind mount goes stale inside the container, and nginx needs a restart in step 4.
- **`"$SCRIPTS/verify.sh"` fails now.** A deploy on top of an existing failure makes both harder to read.

Save the environment fingerprint for step 4:

```bash
"$SCRIPTS/env_snapshot.sh" > "$TMPDIR/env-before.txt"
```

## 2. Tag rollback points, then build

Tag first, because the build moves `:latest`. Only tag and build the services in scope.

```bash
docker tag ai-helpdesk-ai-helpdesk:latest "ai-helpdesk-ai-helpdesk:before-$SHA"                     # staff
docker tag ai-helpdesk-ai-helpdesk-customer:latest "ai-helpdesk-ai-helpdesk-customer:before-$SHA"   # customer
docker tag ai-helpdesk-frontend:latest "ai-helpdesk-frontend:before-$SHA"                           # frontend

EXPORT="$(mktemp -d "$TMPDIR/aihd-deploy.XXXXXX")"
git -C "$ROOT" archive "$SHA" | tar -xf - -C "$EXPORT"

# Both backends, one image. Drop a -t if only one of them is in scope.
docker build --target production --build-arg AI_HELPDESK_RELEASE="$SHA" \
  -t ai-helpdesk-ai-helpdesk:latest -t ai-helpdesk-ai-helpdesk-customer:latest "$EXPORT/backend"

docker build --target production -t ai-helpdesk-frontend:latest "$EXPORT/frontend"
```

Build with `docker build`, not `docker compose build`, from the export. The export has no env files, and `AI_HELPDESK_RELEASE` records which commit is running in the event log.

## 3. Recreate only the named services

Run the dry run first. It should show `Recreate` for the named services and nothing else.

```bash
docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" -p ai-helpdesk \
  up -d --no-deps --no-build --wait --dry-run <services>

docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" -p ai-helpdesk \
  up -d --no-deps --no-build --wait <services>
```

Always name the services. A bare `up -d` also recreates any other service whose definition has drifted, which restarts it for no reason. For the same reason, don't use `scripts/docker-deploy.sh build`, `start` or `restart` on production: they build from the checkout and act on every service.

## 4. Verify

Don't skip this, and don't stop at `--wait` succeeding. Containers can be healthy while traffic goes to the wrong one.

```bash
"$SCRIPTS/verify.sh"

"$SCRIPTS/env_snapshot.sh" > "$TMPDIR/env-after.txt"
diff "$TMPDIR/env-before.txt" "$TMPDIR/env-after.txt"

docker logs --since 10m <container> 2>&1 | grep -E 'Traceback|ERROR'
```

- `verify.sh` must pass, and it must print the new release for the backends you deployed. Its first check is that :80 reaches the customer instance. If only that fails, wait 15 seconds and run it again, since nginx re-resolves the container's address every 10 seconds. If it still fails, check that the checkout is on `main`, then run `docker restart ai-helpdesk-nginx`. `nginx -s reload` isn't enough, because the bind-mounted config can be stale.
- The env diff should show only `AI_HELPDESK_RELEASE`, on the backends you deployed. Anything else is a config change nobody asked for, so report it.
- The logs should have no `Traceback` or `ERROR` from this startup.

`verify.sh` makes no model calls. A real chat smoke test spends money and writes to production's logs, so run one only if the user asks.

If everything passes, remove the export with `rm -rf "$EXPORT"`.

## Roll back

If verification fails and a quick look doesn't explain it, roll back first and investigate afterwards. Customers are on :80. Move `:latest` back for the services in scope, recreate, and verify again:

```bash
docker tag "ai-helpdesk-ai-helpdesk:before-$SHA" ai-helpdesk-ai-helpdesk:latest
docker tag "ai-helpdesk-ai-helpdesk-customer:before-$SHA" ai-helpdesk-ai-helpdesk-customer:latest
docker tag "ai-helpdesk-frontend:before-$SHA" ai-helpdesk-frontend:latest

docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" -p ai-helpdesk \
  up -d --no-deps --no-build --wait <services>

"$SCRIPTS/verify.sh"
```

Keep rollback tags until the user says to remove them.

## Report

Say which services now run which commit, give the rollback tags, and state the `verify.sh` result and the env diff. If something failed or you rolled back, lead with that.
