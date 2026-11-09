"""Owned temporary services, real application entrypoints, empty environments."""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from acceptance.common import DATA, ROOT, read, sha, write


def run(args, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, **kwargs)
    if result.returncode:
        error_dir = kwargs.get("cwd")
        if error_dir:
            Path(error_dir, "acceptance-command-error.log").write_text(result.stderr)
        raise RuntimeError(f"Isolated command failed ({result.returncode}); diagnostics in {error_dir}")
    return result.stdout.strip()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        result = sock.getsockname()[1]
    if result in {5433, 6379}:
        raise ValueError("Production port allocated unexpectedly")
    return result


class Stack:
    def __init__(self, split="development", real=False, frontend=False, simulated_delay=None):
        self.root = Path(tempfile.mkdtemp(prefix="aihd-stage6-", dir="/private/tmp"))
        self.source = self.root / "source"
        self.name = "aihd-stage6-" + uuid.uuid4().hex[:12]
        self.processes, self.containers = [], []
        self.real, self.frontend = real, frontend
        if real and simulated_delay is not None:
            raise ValueError("Real and simulated suppliers cannot share a stack")
        self.simulated_delay = simulated_delay
        self.py = ROOT / "backend/.venv/bin/python"
        self.sqlpy = ROOT / "ai-sql-agent/.venv/bin/python"
        self.ports = {name: port() for name in ("pg", "redis", "api", "sql", "gateway", "frontend")}
        if len(set(self.ports.values())) != len(self.ports):
            raise RuntimeError("Port collision; no services have been created")
        self.origin = f"http://127.0.0.1:{self.ports['frontend']}"
        self.api = f"http://127.0.0.1:{self.ports['api']}"
        self.sql = f"http://127.0.0.1:{self.ports['sql']}"
        self.gateway = f"http://127.0.0.1:{self.ports['gateway']}"
        self.token, self.sqlkey, self.password = (secrets.token_urlsafe(32) for _ in range(3))
        self.cfg = {"root": str(self.root), "ports": self.ports, "gateway_port": self.ports["gateway"],
            "gateway_token": self.token, "password": self.password, "sql_key": self.sqlkey,
            "origin": self.origin, "api": self.api, "sql": self.sql, "split": split,
            "postgres_dsn": f"postgresql://acceptance:isolated@127.0.0.1:{self.ports['pg']}/acceptance",
            "redis_url": f"redis://127.0.0.1:{self.ports['redis']}/0",
            "corpus": str(self.source / "acceptance/data" / DATA.name / (split + ".json")),
            "manifest_sha256": sha((DATA / "manifest.json").read_bytes())}
        self.config = self.root / "private-config.json"
        write(self.config, self.cfg, private=True)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root),
                    "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1", "TMPDIR": str(self.root),
                    "PYTHONPATH": str(self.source) + ":" + str(self.source / "backend") + ":" + str(self.source / "ai-sql-agent/src"),
                    "A2A_CONTRACT_DIR": str(self.source / "a2a-contract"), "A2A_CONTRACT_REQUIRED": "1"}

    def copy(self):
        files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z", "--",
            "backend", "ai-sql-agent", "frontend", "a2a-contract", "acceptance"], cwd=ROOT).decode().split("\0")
        hashes = {}
        for rel in set(files) - {""}:
            src, dest = ROOT / rel, self.source / rel
            if not src.is_file() or src.is_symlink():
                raise ValueError("Unexpected source entry: " + rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            hashes[rel] = sha(src.read_bytes())
        if list(self.source.rglob(".env")):
            raise ValueError("Source export contains an environment file")
        self.fingerprint = sha(hashes)
        write(self.root / "source-manifest.json", {"files": hashes, "sha256": self.fingerprint,
                                                   "revision": run(["git", "rev-parse", "HEAD"], cwd=ROOT)})

    def start_process(self, name, command, env, cwd):
        log = (self.root / (name + ".log")).open("wb")
        process = subprocess.Popen([str(x) for x in command], env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.processes.append((name, process))
        return process

    def wait_http(self, url, seconds=90):
        end = time.monotonic() + seconds
        with httpx.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < end:
                dead = [name for name, proc in self.processes if proc.poll() is not None]
                if dead:
                    raise RuntimeError("Process exited; inspect private stack logs: " + ', '.join(dead))
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.3)
        raise TimeoutError("Stack readiness timed out")

    def start(self):
        self.copy()
        for name, image, args in [
            ("pg", "pgvector/pgvector:pg16", ["-e", "POSTGRES_USER=acceptance", "-e", "POSTGRES_PASSWORD=isolated", "-e", "POSTGRES_DB=acceptance", "--tmpfs", "/var/lib/postgresql/data"]),
            ("redis", "redis:7-alpine", []),
        ]:
            resource = self.name + "-" + name
            internal = 5432 if name == "pg" else 6379
            run(["docker", "run", "-d", "--rm", "--name", resource, "--label", "ai-helpdesk.acceptance=" + self.name,
                 "-p", f"127.0.0.1:{self.ports[name]}:{internal}", *args, image])
            self.containers.append(resource)
        for _ in range(90):
            try:
                run(["docker", "exec", self.name + "-pg", "pg_isready", "-U", "acceptance", "-d", "acceptance"])
                break
            except RuntimeError:
                time.sleep(0.3)
        backend_env = {**self.env, "APP_ENV": "development", "POSTGRES_DSN": self.cfg["postgres_dsn"], "REDIS_URL": self.cfg["redis_url"],
            "LLM_PROVIDER": "openai", "OPENAI_API_KEY": self.token, "OPENAI_BASE_URL": self.gateway + "/helpdesk/v1",
            "OPENAI_MODEL": "gpt-6.1-sol", "OPENAI_API_MODE": "responses", "LLM_MAX_RETRIES": "0", "LLM_TIMEOUT_S": "150",
            "AI_HELPDESK_STRUCTURED_MAX_TOKENS": "2048", "AI_HELPDESK_ANSWER_MAX_TOKENS": "2048", "AI_HELPDESK_ANSWER_EFFORT": "low",
            "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_STAFF_AUTH": "local", "AI_HELPDESK_PUBLIC_ORIGIN": self.origin,
            "CORS_ALLOW_ORIGINS": self.origin,
            "AI_HELPDESK_ATTACHMENT_STORAGE": "local", "AI_HELPDESK_ATTACHMENT_DIR": str(self.root / "objects"),
            "AI_HELPDESK_RUN_MODE": "queued", "AI_HELPDESK_ROLE": "all", "AI_HELPDESK_WORKER_CONCURRENCY": "4",
            "AI_HELPDESK_RUN_MAX_ATTEMPTS": "1", "AI_HELPDESK_RUN_REDISPATCH_S": "30", "AI_HELPDESK_AGENT_REPLICAS": "1",
            "ENABLE_CROSS_SESSION_MEMORY": "false", "EMBEDDING_DIM": "256", "AI_HELPDESK_EVENT_LOG": "full",
            "SQL_AGENT_URL": self.sql, "SQL_AGENT_API_KEY": self.sqlkey, "SQL_AGENT_TIMEOUT_S": "180",
            "ACCEPTANCE_GATEWAY_ORIGIN": self.gateway, "ACCEPTANCE_SQL_ORIGIN": self.sql}
        self.backend_env = backend_env
        seeded = run([self.py, "-m", "acceptance.seed", self.config], env=backend_env, cwd=self.source / "backend")
        write(self.root / "seed-result.json", {"result": seeded})
        keys = {"keys": [{"key": self.sqlkey, "caller": "acceptance-helpdesk", "user_id": "ai-helpdesk", "roles": ["readonly"], "can_impersonate": False,
                          "rate_limit_per_minute": 1000}], "policies": {"readonly": {"allow_tables": [db + ".*" for db in read(self.cfg["corpus"])["records"]]}}}
        write(self.root / "sql-keys.json", keys, private=True)
        sql_env = {**self.env, "AI_SQL_APP_ENV": "development", "AI_SQL_DEPLOYMENT_SCOPE": "internal_single_tenant",
            "AI_SQL_AUTH_MODE": "api_key", "AI_SQL_API_KEYS_FILE": str(self.root / "sql-keys.json"),
            "AI_SQL_MODEL_PROVIDER": "openai", "AI_SQL_OPENAI_API_KEY": self.token, "AI_SQL_OPENAI_BASE_URL": self.gateway + "/sql/v1",
            "AI_SQL_OPENAI_API_MODE": "responses", "AI_SQL_CHAT_MODEL": "gpt-6.1-sol", "AI_SQL_CODER_MODEL": "gpt-6.1-sol",
            "AI_SQL_MAX_OUTPUT_TOKENS": "2048", "AI_SQL_MAX_COMPLETION_TOKENS": "2048", "AI_SQL_HTTP_MAX_RETRIES": "0",
            "AI_SQL_HTTP_TIMEOUT_SECONDS": "150", "AI_SQL_REASONING_EFFORT": "low", "AI_SQL_MODEL_SETTINGS_ENABLED": "false",
            "AI_SQL_EMBEDDING_BACKEND": "local", "AI_SQL_SEMANTIC_DIR": str(self.root / "semantic"),
            "ACCEPTANCE_GATEWAY_ORIGIN": self.gateway, "ACCEPTANCE_SQL_ORIGIN": self.sql}
        gateway_env = {**self.env, "PYTHONPATH": str(ROOT)}
        command = ([self.py, "-m", "acceptance.simulated_provider", "--config", self.config, "--delay", str(self.simulated_delay)]
                   if self.simulated_delay is not None else [self.py, "-m", "acceptance.gateway", "--config", self.config, *(["--real-calibration"] if self.real else [])])
        self.start_process("gateway", command, gateway_env, self.root)
        self.wait_http(self.gateway + "/health")
        self.start_process("sql", [self.sqlpy, "-m", "acceptance.runtime", "ai_sql_agent.api.app:app", str(self.ports["sql"])], sql_env, self.root)
        self.wait_http(self.sql + "/health")
        self.start_process("backend", [self.py, "-m", "acceptance.runtime", "app.api.main:app", str(self.ports["api"])], backend_env, self.source / "backend")
        self.wait_http(self.api + "/livez")
        if self.frontend:
            shutil.copytree(ROOT / "frontend/node_modules", self.source / "frontend/node_modules", symlinks=True)
            env = {**self.env, "VITE_API_URL": self.api}
            self.start_process("frontend", [ROOT / "frontend/node_modules/.bin/vite", "--host", "127.0.0.1", "--port", str(self.ports["frontend"])], env, self.source / "frontend")
            self.wait_http(self.origin)
        write(self.root / "provenance.json", {"source_sha256": self.fingerprint, "manifest_sha256": self.cfg["manifest_sha256"],
              "model": "gpt-6.1-sol", "api_mode": "responses", "max_output_tokens": 2048, "http_retries": 0,
              "helpdesk_embedding": "local-hash-256", "sql_embedding": "local-hash", "sql_rerank": "local-lexical",
              "cross_session_memory": False, "replicas": 1, "worker_concurrency": 4, "redispatch_seconds": 30, "fresh_database": True,
              "fresh_model_retrieval_extraction_caches": True, "real_provider": self.real,
              "provider_mode": "real" if self.real else "simulated" if self.simulated_delay is not None else "disabled",
              "simulated_delay_seconds": self.simulated_delay,
              "owned_resources": self.containers, "ports": self.ports, "production_touched": False})
        return self

    def close(self):
        import signal
        for _, process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for resource in reversed(self.containers):
            # Only exact names created by this object; never global pruning.
            subprocess.run(["docker", "rm", "-f", resource], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-calibration", action="store_true")
    parser.add_argument("--frontend", action="store_true")
    parser.add_argument("--simulated-delay", type=float)
    args = parser.parse_args()
    stack = Stack(real=args.real_calibration, frontend=args.frontend, simulated_delay=args.simulated_delay)
    try:
        stack.start()
        print(str(stack.root), flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.close()


if __name__ == "__main__":
    main()
