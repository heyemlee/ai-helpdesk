"""Acceptance bridge that executes the actual immutable application images.

Only tracing/seed helpers and synthetic data are mounted; no application source
is mounted over the image. All host ports and state are owned, disposable and
loopback-only. Real supplier access requires a separate release budget.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx

from acceptance.common import DATA, ROOT, dataset, read, sha, write
from acceptance.seed import business
from acceptance.stack import port


from acceptance.configuration import CONFIGURATION


class ContainerStack:
    def __init__(self, manifest, *, split="release", provider="disabled", budget=None, baseline=False, delay=.8):
        if provider not in {"disabled", "simulated", "real"}:
            raise ValueError("Unknown provider mode")
        self.manifest = read(manifest) if isinstance(manifest, (str, Path)) else manifest
        if set(self.manifest.get("images", {})) != {"backend", "sql", "frontend"}:
            raise ValueError("Three immutable image IDs are required")
        for service, identifier in self.manifest["images"].items():
            if not identifier.startswith("sha256:"):
                raise ValueError("Local tested image IDs are required")
            info = __import__("json").loads(subprocess.check_output(["docker", "image", "inspect", identifier]))[0]
            if info["Id"] != identifier or info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != self.manifest["revision"]:
                raise ValueError("Image content/revision mismatch: " + service)
        if baseline and not self.manifest["revision"].startswith("5310da9"):
            raise ValueError("Baseline must be built from commit 5310da9")
        self.provider, self.baseline, self.split = provider, baseline, split
        self.delay = delay
        self.root = Path(tempfile.mkdtemp(prefix="aihd-stage6-images-", dir="/private/tmp"))
        self.name = "aihd-stage6-images-" + uuid.uuid4().hex[:10]
        self.ports = {k: port() for k in ("pg", "redis", "api", "sql", "gateway", "frontend")}
        if len(set(self.ports.values())) != len(self.ports):
            raise ValueError("Host port collision")
        self.password, self.token, self.sqlkey, pg_password = (secrets.token_urlsafe(32) for _ in range(4))
        self.origin = f"http://127.0.0.1:{self.ports['frontend']}"
        self.cfg = {"root": str(self.root), "split": split, "ports": self.ports, "password": self.password,
                    "api": f"http://127.0.0.1:{self.ports['api']}", "sql": f"http://127.0.0.1:{self.ports['sql']}",
                    "origin": self.origin, "gateway_port": self.ports["gateway"], "gateway_token": self.token,
                    "postgres_dsn": f"postgresql://acceptance:{pg_password}@127.0.0.1:{self.ports['pg']}/acceptance",
                    "redis_url": f"redis://127.0.0.1:{self.ports['redis']}/0", "sql_key": self.sqlkey,
                    "manifest_sha256": sha((DATA / "manifest.json").read_bytes())}
        self.compose = ["docker", "compose", "-p", self.name, "-f", str(self.root / "compose.json")]
        helpers = self.root / "harness" / "acceptance"
        shutil.copytree(ROOT / "acceptance", helpers, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.harness_sha = sha({str(p.relative_to(helpers)): sha(p.read_bytes()) for p in sorted(helpers.rglob("*.py"))})
        self.corpus = dataset()[1 if split == "development" else 2]
        business(self.root, self.corpus, ROOT / "ai-sql-agent", container_directory="/business")
        for folder in ("state", "state/objects", "state/sql", "state/config", "provider-state"):
            (self.root / folder).mkdir(exist_ok=True)
            (self.root / folder).chmod(0o777)
        write(self.root / "private-config.json", self.cfg, private=True)
        write(self.root / "provider-state/config.json", {**self.cfg, "root": "/provider-state", "gateway_port": 8000,
              "corpus": "/opt/harness/acceptance/data/" + DATA.name + "/" + split + ".json"})
        write(self.root / "state/config/keys.json", {"keys": [{"key": self.sqlkey, "caller": "acceptance-helpdesk", "user_id": "ai-helpdesk", "roles": ["readonly"], "can_impersonate": False, "rate_limit_per_minute": 1000}],
              "policies": {"readonly": {"allow_tables": [db + ".*" for db in self.corpus["records"]]}}})
        helper_mount = str(self.root / "harness") + ":/opt/harness:ro"
        gateway = "http://provider:8000"
        common = {"PYTHONPATH": "/opt/harness:/app", "ACCEPTANCE_BIND_HOST": "0.0.0.0",
                  "ACCEPTANCE_GATEWAY_ORIGIN": gateway, "ACCEPTANCE_SQL_ORIGIN": "http://sql:8000"}
        backend_env = {**common, "APP_ENV": "development", "POSTGRES_DSN": f"postgresql://acceptance:{pg_password}@pg:5432/acceptance",
                       "REDIS_URL": "redis://redis:6379/0", "LLM_PROVIDER": "openai", "OPENAI_BASE_URL": gateway + "/helpdesk/v1",
                       "OPENAI_API_KEY": self.token, "OPENAI_MODEL": "gpt-6.1-sol", "OPENAI_API_MODE": "responses", "LLM_MAX_RETRIES": "0",
                       "LLM_TIMEOUT_S": "150", "AI_HELPDESK_STRUCTURED_MAX_TOKENS": "2048", "AI_HELPDESK_ANSWER_MAX_TOKENS": "2048",
                       "AI_HELPDESK_ANSWER_EFFORT": "low", "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_STAFF_AUTH": "off" if baseline else "local",
                       "AI_HELPDESK_PUBLIC_ORIGIN": self.origin, "CORS_ALLOW_ORIGINS": self.origin,
                       "AI_HELPDESK_RUN_MODE": "queued", "AI_HELPDESK_RUN_MAX_ATTEMPTS": "1", "AI_HELPDESK_RUN_REDISPATCH_S": "30",
                       "AI_HELPDESK_WORKER_CONCURRENCY": "4", "AI_HELPDESK_AGENT_REPLICAS": "1", "AI_HELPDESK_RUN_DRAIN_S": "2",
                       "AI_HELPDESK_DEGRADE_BACKLOG": "20,40,80,160",
                       "AI_HELPDESK_ATTACHMENT_STORAGE": "off" if baseline else "local", "AI_HELPDESK_ATTACHMENT_DIR": "/state/objects",
                       "AI_HELPDESK_MODEL_SETTINGS_PATH": "/state/config/model_settings.json", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                       "ENABLE_CROSS_SESSION_MEMORY": "false", "EMBEDDING_DIM": "256", "AI_HELPDESK_EVENT_LOG": "full",
                       "SQL_AGENT_URL": "http://sql:8000", "SQL_AGENT_API_KEY": self.sqlkey, "SQL_AGENT_TIMEOUT_S": "180"}
        if baseline:
            backend_env.update(AI_HELPDESK_API_KEY=self.token, AI_HELPDESK_PROTECT_CHAT="true")
        sql_env = {**common, "AI_SQL_APP_ENV": "development", "AI_SQL_DEPLOYMENT_SCOPE": "internal_single_tenant",
                   "AI_SQL_AUTH_MODE": "api_key", "AI_SQL_API_KEYS_FILE": "/state/config/keys.json", "AI_SQL_MODEL_PROVIDER": "openai",
                   "AI_SQL_OPENAI_API_KEY": self.token, "AI_SQL_OPENAI_BASE_URL": gateway + "/sql/v1", "AI_SQL_OPENAI_API_MODE": "responses",
                   "AI_SQL_CHAT_MODEL": "gpt-6.1-sol", "AI_SQL_CODER_MODEL": "gpt-6.1-sol", "AI_SQL_MAX_OUTPUT_TOKENS": "2048",
                   "AI_SQL_MAX_COMPLETION_TOKENS": "2048", "AI_SQL_HTTP_MAX_RETRIES": "0", "AI_SQL_HTTP_TIMEOUT_SECONDS": "150",
                   "AI_SQL_REASONING_EFFORT": "low", "AI_SQL_MODEL_SETTINGS_ENABLED": "false", "AI_SQL_EMBEDDING_BACKEND": "local",
                   "AI_SQL_SEMANTIC_DIR": "/semantic", "AI_SQL_MEMORY_DB_PATH": "/state/sql/memory.db",
                   "AI_SQL_AUDIT_DB_PATH": "/state/sql/audit.db", "AI_SQL_RUNS_DB_PATH": "/state/sql/runs.db"}
        def published(name, internal):
            return [f"127.0.0.1:{self.ports[name]}:{internal}"]
        def health(path):
            return {"test": ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/" + path + "',timeout=2)"], "interval": "2s", "timeout": "3s", "retries": 60}
        backend = {"image": self.manifest["images"]["backend"], "environment": backend_env,
                   "volumes": [helper_mount, str(self.root / "state") + ":/state"],
                   "command": ["python", "-m", "acceptance.runtime", "app.api.main:app", "8000"], "healthcheck": health("readyz")}
        provider_command = ["python", "-m", "acceptance.simulated_provider", "--config", "/provider-state/config.json", "--delay", str(delay)] if provider == "simulated" else ["python", "-m", "acceptance.gateway", "--config", "/provider-state/config.json"]
        provider_volumes = [helper_mount, str(self.root / "provider-state") + ":/provider-state"]
        if provider == "real":
            from acceptance.gateway import release_ledger
            if not budget:
                raise ValueError("Real release provider requires a separately approved budget file")
            # Docker cannot create nested mount destinations under the read-only
            # harness bind. Prepare empty targets, never copies of credentials.
            (helpers.parent / ".stage6").mkdir()
            settings_target = helpers.parent / "backend/data/runtime/model_settings.json"
            settings_target.parent.mkdir(parents=True)
            settings_target.touch(mode=0o600, exist_ok=False)
            approval = read(budget)
            release_ledger(approval)
            write(self.root / "provider-state/approval.json", approval)
            provider_command += ["--release-budget", "/provider-state/approval.json"]
            provider_volumes += [str(ROOT / ".stage6") + ":/opt/harness/.stage6",
                                 str(ROOT / "backend/data/runtime/model_settings.json") + ":/opt/harness/backend/data/runtime/model_settings.json:ro"]
        self.spec = {"services": {
            "pg": {"image": "pgvector/pgvector:pg16", "environment": {"POSTGRES_USER": "acceptance", "POSTGRES_PASSWORD": pg_password, "POSTGRES_DB": "acceptance"}, "ports": published("pg", 5432), "tmpfs": ["/var/lib/postgresql/data"],
                   "healthcheck": {"test": ["CMD", "pg_isready", "-U", "acceptance"], "interval": "2s", "retries": 30}},
            "redis": {"image": "redis:7-alpine", "ports": published("redis", 6379), "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "2s", "retries": 30}},
            "provider": {"image": self.manifest["images"]["backend"], "user": str(os.getuid()), "environment": {**common}, "volumes": provider_volumes, "command": provider_command, "ports": published("gateway", 8000), "healthcheck": health("health")},
            "sql": {"image": self.manifest["images"]["sql"], "environment": sql_env, "volumes": [helper_mount, str(self.root / "state") + ":/state", str(self.root / "business") + ":/business:ro", str(self.root / "semantic") + ":/semantic:ro"], "ports": published("sql", 8000), "command": ["python", "-m", "acceptance.runtime", "ai_sql_agent.api.app:app", "8000"], "healthcheck": health("health")},
            "api": {**backend, "environment": {**backend_env, "AI_HELPDESK_ROLE": "api"}, "ports": published("api", 8000)},
            "worker": {**backend, "environment": {**backend_env, "AI_HELPDESK_ROLE": "worker"}},
            "frontend": {"image": self.manifest["images"]["frontend"], "environment": {"API_UPSTREAM": "api:8000", "CONSOLE_PUBLIC_HOST": "employees.invalid"}, "ports": published("frontend", 80)}
        }}
        write(self.root / "compose.json", self.spec, private=True)

    def run(self, *args):
        result = subprocess.run([*self.compose, *args], capture_output=True, text=True)
        with (self.root / "compose.log").open("a") as file:
            file.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Isolated image command failed; see " + str(self.root / "compose.log"))
        return result.stdout

    def start(self):
        self.run("up", "-d", "--wait", "pg", "redis")
        self.run("run", "--rm", "--no-deps", "-T", "-e", "ACCEPTANCE_BOOTSTRAP_PASSWORD=" + self.password,
                 "api", "python", "-m", "acceptance.container_stack", "--bootstrap", *( ["--baseline"] if self.baseline else []))
        self.run("up", "-d", "--wait", "provider", "sql", "api", "worker", "frontend")
        observed = {}
        for name, service in (("backend", "api"), ("frontend", "frontend"), ("sql", "sql")):
            container = self.run("ps", "-q", service).strip()
            observed[name] = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", container]).decode().strip()
        if observed != self.manifest["images"]:
            raise ValueError("Running containers differ from candidate images")
        worker = self.run("ps", "-q", "worker").strip()
        if subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", worker]).decode().strip() != observed["backend"]:
            raise ValueError("Worker runs another image")
        self.provenance = {**CONFIGURATION, "execution": "immutable_containers", "artifacts": {"revision": self.manifest["revision"], "images": observed},
                           "config_sha256": sha(CONFIGURATION), "manifest_sha256": self.cfg["manifest_sha256"], "harness_sha256": self.harness_sha,
                           "stack_id": self.name, "fresh_database": True, "fresh_caches": True, "provider_mode": self.provider,
                           "real_provider": self.provider == "real", "production_touched": False, "ports": self.ports,
                           "simulated_delay_seconds": self.delay if self.provider == "simulated" else None}
        info = __import__("json").loads(subprocess.check_output(["docker", "info", "--format", "{{json .}}" ]))
        self.provenance["docker_resources"] = {k: info.get(k) for k in ("NCPU", "MemTotal", "Architecture", "OSType", "ServerVersion")}
        write(self.root / "provenance.json", self.provenance)

    def close(self):
        try:
            (self.root / "containers.log").write_text(self.run("logs", "--no-color"))
        finally:
            self.run("down", "--volumes", "--remove-orphans", "--timeout", "5")


async def bootstrap(baseline):
    from app.storage.pg import Database
    db = Database(os.environ["POSTGRES_DSN"])
    await db.connect(256)
    if not baseline:
        from app.core.staff_auth import StaffAuth
        auth = StaffAuth(db, "staff", origin=os.environ["AI_HELPDESK_PUBLIC_ORIGIN"])
        await auth.create_account("acceptance-admin", os.environ["ACCEPTANCE_BOOTSTRAP_PASSWORD"], "admin", bootstrap=True)
    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--provider", choices=("disabled", "simulated", "real"), default="disabled")
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--split", choices=("development", "release"), default="development")
    parser.add_argument("--simulated-delay", type=float, default=.8)
    args = parser.parse_args()
    if args.bootstrap:
        asyncio.run(bootstrap(args.baseline))
    else:
        stack = ContainerStack(args.manifest, split=args.split, provider=args.provider, budget=args.budget, baseline=args.baseline, delay=args.simulated_delay)
        try:
            stack.start()
            print(stack.root, flush=True)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stack.close()
